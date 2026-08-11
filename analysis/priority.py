"""Turn Pass A's observations into scalar node properties that Cypher can rank on.

WHY THIS MODULE EXISTS AT ALL
Summaries are stored as a JSON string on the Function node (store.write_summaries).
Cypher cannot read into a JSON string, so nothing in a summary can participate in a
query that has to ORDER BY ... LIMIT before the results come back to Python. That is
exactly the ordering that decides which paths get spent on. Projecting a handful of
scalars next to the JSON is what makes summary knowledge available to path selection
instead of only after it.

THE MODEL OBSERVES, THIS FILE JUDGES
`risk.reasons` is an enum list of things visible in one file. The importance SCORE is
computed here, from weights that live in code where they can be measured and tuned.
That split is deliberate and it is the calibration guard: a model asked "is this
important?" says yes to almost everything, because a per-file reader has no baseline
to compare against. A model asked "does this concatenate SQL?" answers accurately.
So the model is never asked for the judgment, only for the observations behind it.

WHAT IS DELIBERATELY *NOT* IN THE SCORE
Blast radius (caller count) and exposure (reachable from an annotated endpoint) are
graph facts, not summary facts. They belong to path scoring, where the graph is
already being traversed. Keeping node-level signal purely summary-derived means these
properties are written once by Pass A and stay valid across every later re-run of
reach/paths — re-marking reachability does not invalidate them.

SIGNALS ADD RECALL, THEY NEVER REMOVE IT
Every consumer of these properties must union them with its structural equivalent,
never intersect. A missed `is_entry_point` should cost nothing, because the annotation
seed still fires; if selection required the model's flag, a false negative here would
silently delete paths that no later pass could recover. See reach.mark_from_entry.
"""
from __future__ import annotations

import json

from . import contract
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Weight per observable. Tuned against the corpus manifest, not guessed: raise a
# weight only when a measured miss justifies it, and re-run scripts/run_analysis.py
# to confirm the ground-truth scores did not move the wrong way.
RISK_WEIGHTS = {
    "builds_sql_dynamically": 3.0,
    "deserialization": 3.0,
    "spawns_process": 3.0,
    "parses_untrusted_input": 2.0,
    "manual_resource_handling": 2.0,
    "reflection": 2.0,
    "crypto": 1.5,
    "writes_filesystem": 1.5,
    "transaction_boundary": 1.0,
    # An auth check is not a vulnerability — it is a place where getting it wrong
    # matters, which is why it scores at all rather than scoring high.
    "auth_check": 1.0,
    "authz_check": 1.0,
    "none": 0.0,
}

# flows_to values that mean a parameter reaches something dangerous. 'return' and
# 'field:*' are flows but not sinks, so they are not here.
_DANGEROUS_FLOWS = {"sql", "exec", "file", "response"}

# SEVERITY, COMPUTED — the model reports certainty and impact, this decides.
#
# Read down a column: the same defect drops a level for every step you take away
# from being able to demonstrate it. That is the "certainty first, then impact"
# rule the old prompt had to explain in prose, made structural — a model can
# forget a rubric, a table cannot.
#
# `quality` is low at every certainty on purpose. A definitely-slow loop should
# never outrank a maybe-exploitable injection, so an optimization finding can be
# reported in full without ever crowding a security report.
#
# Tune by moving a cell, then run backfill_signals() — the whole repo re-scores
# with no model calls. That is the entire reason this is not a model field.
SEVERITY = {
    ("demonstrated", "exposure"):    "critical",
    ("demonstrated", "integrity"):   "high",
    ("demonstrated", "correctness"): "high",
    ("demonstrated", "quality"):     "low",
    ("probable", "exposure"):        "high",
    # A leak or weakness you cannot demonstrate is a suggestion, not a task.
    # demonstrated+integrity stays high, so real leaks still surface; this only
    # moves the ones the model could not show.
    ("probable", "integrity"):       "medium",
    ("probable", "correctness"):     "medium",
    ("probable", "quality"):         "low",
    ("speculative", "exposure"):     "medium",
    ("speculative", "integrity"):    "low",
    # LOW MUST BE REACHABLE WITHOUT `quality`. Measured across 9 files and 34
    # findings, impact=quality fired ZERO times, so with this cell at "medium"
    # the low band was structurally unreachable and severity collapsed to three
    # levels. It is also the more honest reading: "I am inferring this might
    # behave wrongly" is a suggestion, not a defect. correctness was 56% of all
    # findings, so this is the axis that actually populates the band.
    ("speculative", "correctness"):  "low",
    ("speculative", "quality"):      "low",
}


def severity(certainty: str | None, impact: str | None) -> str:
    """(certainty, impact) -> critical | high | medium | low.

    Unknown or missing values return 'none' rather than guessing a level. A
    finding that reaches the report with no severity is visible; one silently
    defaulted to 'medium' is not, and would quietly inflate every ranking.
    """
    key = (str(certainty or "").lower(), str(impact or "").lower())
    return SEVERITY.get(key, "none")


def apply_severity(finding: dict) -> dict:
    """Stamp a computed `severity` onto a finding, in place.

    Called wherever a finding is built — path_pass, the single-file path, join
    candidates. Downstream (findings.py's ranking, adversarial_pass's prompt)
    keeps reading `severity` and never has to know it is derived.
    """
    finding["severity"] = severity(finding.get("certainty"), finding.get("impact"))
    return finding


_IMPORTANT_THRESHOLD = 3.0

# Above this share of the repo, `important` is not selecting anything. Reported
# loudly rather than left for someone to notice in a dashboard: an uncalibrated
# flag that passes everything looks identical to a filter that is working.
_FLAG_RATE_CEILING = 0.30


def _bare_names(values) -> list[str]:
    """['DriverManager.getConnection', 'get()'] -> ['get', 'getconnection'] ... no:
    -> ['getConnection', 'get']. Strips any receiver prefix and call parens, dedupes,
    and drops empties. Case is preserved because Function.name is case-sensitive."""
    out = set()
    for v in values or []:
        name = str(v).split("(", 1)[0].rsplit(".", 1)[-1].strip()
        if name:
            out.add(name)
    return sorted(out)


def derive_signals(summary: dict) -> dict:
    """Flat scalars for one summary. Pure function — no store, no I/O, easy to test."""
    db = summary.get("db") or {}
    src = summary.get("source") or {}
    guards = summary.get("guards") or {}
    contracts = summary.get("contracts") or {}

    # risk.reasons is gone from the schema (it duplicated db{} and touches[]).
    # Reconstructed from the fields that already carried the same facts, so
    # RISK_WEIGHTS keeps working and the scores stay comparable across versions.
    touches = {t for t in (summary.get("touches") or []) if t and t != "none"}
    reasons = []
    if db.get("sql_is_dynamic"):
        reasons.append("builds_sql_dynamically")
    if db.get("acquires"):
        reasons.append("manual_resource_handling")
    for t, r in (("exec", "spawns_process"), ("deserialize", "deserialization"),
                 ("reflection", "reflection"), ("file", "writes_filesystem")):
        if t in touches:
            reasons.append(r)
    if src.get("reads_untrusted"):
        reasons.append("parses_untrusted_input")
    if guards.get("authenticates") or guards.get("authorizes"):
        reasons.append("auth_check")

    # A parameter that reaches a sink WITHOUT being validated. Both halves matter:
    # a validated parameter reaching SQL is a parameterized query, which is the
    # correct pattern and must not score.
    # params[].flows_to is gone; path_pass reads real source and traces the
    # parameter itself. Kept at 0 so the score formula and every stored property
    # keep their shape rather than needing a migration.
    taint_params = 0

    # The exception-path leak. `throws_between_acquire_and_release` deliberately does
    # NOT appear here, and that is the whole subtlety: it is true of any try/finally
    # doing real work, because something can always throw — which is precisely what
    # the finally is for. OR-ing it in flags every correctly-written DAO, which was
    # measured on the corpus (a clean finally-close scored 7.5/important). It is
    # evidence only when the release is NOT guaranteed, where it upgrades a suspicion
    # into a demonstrated leak.
    acquires = bool(db.get("acquires"))
    unguarded = acquires and not db.get("released_in_finally")
    leak = acquires and (unguarded or bool(db.get("resources_leaked")))
    confirmed_leak = unguarded and bool(db.get("throws_between_acquire_and_release"))

    # Counted by COMPUTED severity, not by the model's own confidence. An
    # `impact: quality` finding is never "strong" no matter how certain the model
    # is about it — which is what keeps a file full of style nits from scoring as
    # high-risk.
    strong_findings = sum(
        1 for f in (summary.get("findings") or [])
        if severity(f.get("certainty"), f.get("impact")) in ("critical", "high", "medium")
    )

    score = sum(RISK_WEIGHTS.get(r, 0.5) for r in reasons)
    if db.get("sql_is_dynamic"):
        score += 2.0
    if leak:
        score += 2.0
    if confirmed_leak:
        score += 1.0
    if src.get("reads_untrusted"):
        score += 1.5
    if src.get("is_entry_point"):
        score += 1.0
    score += min(taint_params, 3) * 1.0
    score += min(strong_findings, 3) * 2.0

    # A sanitizer is a control, not a risk. It still carries its observations (an
    # escaper legitimately builds strings) but should not rank as a target.
    if guards.get("is_sanitizer"):
        score *= 0.5

    return {
        "sig_schema_version": contract.SCHEMA_VERSION,
        "sig_risk_score": round(score, 2),
        "sig_important": score >= _IMPORTANT_THRESHOLD,
        "sig_reasons": sorted(reasons),
        "sig_entry": bool(src.get("is_entry_point")),
        "sig_untrusted": bool(src.get("reads_untrusted")),
        "sig_source_kinds": sorted({k for k in (src.get("kinds") or []) if k and k != "none"}),
        "sig_sanitizer": bool(guards.get("is_sanitizer")),
        "sig_auth": bool(guards.get("authenticates") or guards.get("authorizes")),
        "sig_validates": bool(guards.get("validates_input")),
        "sig_taint_params": taint_params,
        "sig_sql_dynamic": bool(db.get("sql_is_dynamic")),
        "sig_acquires": acquires,
        "sig_leak": leak,
        "sig_confirmed_leak": confirmed_leak,
        "sig_findings": strong_findings,
        # ---- contract fields: projected for analysis/join.py, not for scoring ----
        # These deliberately carry NO weight in the score above. A method that can
        # return null is not risky — it is completely ordinary. It only becomes a
        # defect when paired with a CALLER that does not check, and that pairing is
        # a graph question this file cannot see. Scoring them here would rank half
        # the repo as dangerous for writing `return null`.
        #
        # Lists stay flat lists of strings so Cypher can test membership; anything
        # nested would be as unqueryable as the summary JSON these are extracted
        # from, which is the whole reason this module exists.
        "sig_may_return_null": bool(contracts.get("may_return_null")),
        "sig_null_condition": str(contracts.get("null_condition") or ""),
        "sig_returns_sentinel": str(contracts.get("returns_sentinel") or ""),
        # NORMALIZED TO BARE METHOD NAMES, in code rather than by instruction.
        # prompts.py already says "bare method name only, no class prefix" and a
        # measured Nova run returned 'config.get' and
        # 'DriverManager.getConnection' regardless. join.py matches on
        # `callee.name`, which is bare, so a qualified entry does not merely rank
        # lower — it silently never matches and the candidate is lost. A prompt
        # cannot be relied on for a value another query joins against.
        "sig_unguarded_calls": _bare_names(contracts.get("unguarded_calls")),
        "sig_swallowed_calls": _bare_names(contracts.get("swallowed_exception_calls")),
        # Projected separately from sig_leak because the JOIN needs the raw fact,
        # not the derived one: sig_leak already folds in resources_leaked, and the
        # cross-function question is specifically "did THIS function guarantee the
        # release", asked independently of the caller and the callee.
        "sig_released_in_finally": bool(db.get("released_in_finally")),
    }


def signal_rates(store, repo: str) -> dict:
    """How often each flag fires across the repo, as a calibration check.

    Run after Pass A and READ IT. If `important_rate` is above 30% the flag is not
    discriminating and path selection built on it is selecting nothing — that is a
    prompt problem, and it is invisible unless measured. A rate near zero is the
    opposite failure and equally worth catching.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.sig_schema_version IS NOT NULL
        RETURN count(f) AS scored,
               sum(CASE WHEN f.sig_important THEN 1 ELSE 0 END) AS important,
               sum(CASE WHEN f.sig_entry THEN 1 ELSE 0 END) AS entry,
               sum(CASE WHEN f.sig_untrusted THEN 1 ELSE 0 END) AS untrusted,
               sum(CASE WHEN f.sig_sanitizer THEN 1 ELSE 0 END) AS sanitizer,
               sum(CASE WHEN f.sig_leak THEN 1 ELSE 0 END) AS leak,
               sum(CASE WHEN f.sig_sql_dynamic THEN 1 ELSE 0 END) AS sql_dynamic,
               avg(f.sig_risk_score) AS avg_score
        """,
        repo=repo,
    )
    out = dict(rows[0]) if rows else {}
    scored = int(out.get("scored") or 0)
    if not scored:
        _log.warning("[priority] no scored functions — Pass A has not run under "
                     "schema v%s yet", contract.SCHEMA_VERSION)
        return out
    rate = (out.get("important") or 0) / scored
    out["important_rate"] = round(rate, 3)
    out["avg_score"] = round(float(out.get("avg_score") or 0), 2)
    if rate > _FLAG_RATE_CEILING:
        _log.warning(
            "[priority] `important` fired on %.0f%% of %s functions — the signal is "
            "NOT discriminating and any selection built on it is effectively passing "
            "everything through. Tighten the risk.reasons guidance in prompts.py or "
            "raise _IMPORTANT_THRESHOLD; do not treat this run's ranking as meaningful.",
            rate * 100, scored,
        )
    elif rate < 0.01:
        _log.warning(
            "[priority] `important` fired on only %.1f%% of %s functions — verify "
            "this is real scarcity and not the model returning ['none'] by default.",
            rate * 100, scored,
        )
    else:
        _log.info("[priority] signal rates: %s", out)
    return out


def stale_schema(store, repo: str) -> int:
    """Functions with a fresh summary written under an OLDER schema version.

    The failure this catches: body_hash matches, so needs_summary() calls the summary
    fresh, but it predates the fields a later pass reads — which surfaces as that pass
    finding nothing rather than as an error. Counted so a schema bump can re-summarize
    exactly these instead of the whole repo.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary_json IS NOT NULL AND f.summary_hash = f.body_hash
          AND coalesce(f.sig_schema_version, 0) < $version
        RETURN count(f) AS n
        """,
        repo=repo, version=contract.SCHEMA_VERSION,
    )
    n = int(rows[0]["n"]) if rows else 0
    if n:
        _log.warning(
            "[priority] %s function(s) have a current summary from schema v<%s. They "
            "will read as fresh while missing the new fields — re-run Pass A with "
            "force_schema=True to refresh exactly these.", n, contract.SCHEMA_VERSION)
    return n


def backfill_signals(store, repo: str) -> int:
    """Re-derive signals from summaries already stored, without calling any model.

    For changing WEIGHTS. Tuning RISK_WEIGHTS or the threshold does not need new
    summaries — the observations are unchanged, only the arithmetic over them is —
    so this re-scores the repo for free. Without it, every weight experiment would
    look like it needs a re-summarization run.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary_json IS NOT NULL
        RETURN f.id AS id, f.summary_json AS summary
        """,
        repo=repo,
    )
    payload = []
    for row in rows:
        try:
            summary = json.loads(row["summary"])
        except (ValueError, TypeError):
            continue
        payload.append({"id": row["id"], "sig": derive_signals(summary)})
    if not payload:
        return 0
    store._run(
        """
        UNWIND $rows AS row
        MATCH (f:CodeNode {id: row.id})
        SET f += row.sig
        """,
        rows=payload,
    )
    _log.info("[priority] re-derived signals for %s function(s)", len(payload))
    return len(payload)

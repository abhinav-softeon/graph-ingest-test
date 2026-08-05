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

_IMPORTANT_THRESHOLD = 3.0

# Above this share of the repo, `important` is not selecting anything. Reported
# loudly rather than left for someone to notice in a dashboard: an uncalibrated
# flag that passes everything looks identical to a filter that is working.
_FLAG_RATE_CEILING = 0.30


def derive_signals(summary: dict) -> dict:
    """Flat scalars for one summary. Pure function — no store, no I/O, easy to test."""
    db = summary.get("db") or {}
    src = summary.get("source") or {}
    risk = summary.get("risk") or {}
    guards = summary.get("guards") or {}

    reasons = [r for r in (risk.get("reasons") or []) if r and r != "none"]

    # A parameter that reaches a sink WITHOUT being validated. Both halves matter:
    # a validated parameter reaching SQL is a parameterized query, which is the
    # correct pattern and must not score.
    taint_params = sum(
        1 for p in (summary.get("params") or [])
        if not p.get("validated")
        and _DANGEROUS_FLOWS & set(p.get("flows_to") or [])
    )

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

    strong_findings = sum(
        1 for f in (summary.get("findings") or [])
        if str(f.get("confidence") or "").lower() in ("high", "medium")
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

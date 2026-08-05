"""Every function judged with its 1- and 2-hop callee summaries. Path-independent.

THE COVERAGE HOLE THIS FILLS
Pass B only ever sees functions that lie on an enumerated entry->sink path. On the
corpus that universe was 145 of 338 functions — 43%. The other 57% were looked at
exactly once, by Pass A, one file at a time, with no view of what they call. A defect
that needs two frames to see (this function opens a resource, the callee it hands it
to can throw) is invisible to Pass A and never reaches Pass B unless the function
happens to sit between an entry point and a classified sink.

WHY NEIGHBOURHOODS SCALE WHERE PATHS DO NOT
Path enumeration is combinatorial — branching^depth, which is why it needs a bound, a
hub exclusion list and a LIMIT. A neighbourhood is bounded by construction: one call
per function, each carrying that function plus its callees to depth 2. Cost is linear
in the repo and predictable before the run. It also needs NO entry points, so it
still works on a repo whose entry convention `reach.ENTRY_ANNOTATIONS` does not know.

IT REPORTS ONLY THE DELTA
Pass A already reported every defect visible in a single body, and those findings are
passed in and shown per function as ALREADY KNOWN. This pass is asked for what Pass A
structurally could not see: defects arising from this function's interaction with the
functions it calls. Without that, the two passes rediscover the same leak, both
findings go to the adjudicator, and the duplicate is only removed at the very end —
after paying three verifier calls for it. Measured on the corpus: 496 findings reached
Pass D and only 171 were distinct.

WHAT IT IS NOT
It cannot answer taint. "Is this value attacker-controlled" depends on callers, which
a callee-closure deliberately excludes. Injection stays with the path pipeline, where
sanitizer interposition is measurable. This pass is for defects whose evidence is
local but wider than one file.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, contract, store as astore
from .llm import get_client
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

SYSTEM = """\
You are reviewing one function together with the functions it calls, to depth 2.

You get the target function's structured summary and the summaries of its callees and
their callees. This is a NEIGHBOURHOOD, not a path: you are not tracing untrusted data
from an entry point, you are asking whether this function is correct given what it
calls.

WHAT TO LOOK FOR — defects that need more than one frame to see:
- A resource acquired here and handed to a callee that can throw, with no finally.
- A resource returned by a callee that this function never releases. If a callee's
  summary says it RETURNS a Connection/Stream/Session, whoever received it owns
  closing it.
- A release that happens only inside a callee on some paths.
- An invariant this function assumes that a callee does not guarantee.
- Error handling that swallows a failure a callee reports.

WHAT NOT TO REPORT
- Anything listed under ALREADY REPORTED for the target. Those were found by a pass
  that read the full source of this function. Repeating one is not a second finding,
  it is the same finding costing another round of adjudication. Say nothing about it.
- Anything visible in the target's own body ALONE, reported or not. If you would
  reach the same conclusion without looking at a single callee, it is out of scope.
- Anything about attacker-controlled input. You cannot see this function's callers, so
  you cannot know what is attacker-controlled. A separate pass owns that with the
  caller context you do not have.

THE TEST FOR WHETHER SOMETHING BELONGS HERE
Would this defect still be visible if every callee were replaced by a stub that does
nothing and returns a default? If yes, it is single-body and not yours. If no — the
bug depends on what a callee actually does, returns, or throws — it is exactly what
this pass exists for, and it is invisible to every other pass.

Set `is_defect` true only when you can name the frame and the reason. If you need a
body you were not given, name it in `need_source_for` and set the flags false — the
source will be fetched and you will be asked again. Prefer that over guessing."""


@dataclass
class NeighborhoodReport:
    functions_considered: int = 0
    neighborhoods_sent: int = 0
    skipped_no_callees: int = 0
    skipped_no_summary: int = 0
    targets_with_prior: int = 0   # targets shown an ALREADY REPORTED list
    calls_made: int = 0
    batches_rejected: int = 0
    defects: int = 0
    needs_expansion: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "functions_considered": self.functions_considered,
            "neighborhoods_sent": self.neighborhoods_sent,
            "skipped_no_callees": self.skipped_no_callees,
            "skipped_no_summary": self.skipped_no_summary,
            "targets_with_prior": self.targets_with_prior,
            "calls_made": self.calls_made,
            "batches_rejected": self.batches_rejected,
            "defects": self.defects,
            "needs_expansion": self.needs_expansion,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "errors": self.errors[:20],
            "seconds": round(self.seconds, 1),
        }


def build(store, repo: str, hops: int = 2) -> list[dict]:
    """One neighbourhood per function: itself plus its callee closure to `hops`.

    Trusted edges only — a bare-name CALLS edge sits near 5% precision, so a
    neighbourhood assembled through one is mostly fiction and would make the model
    reason about callees that are not really there.
    """
    rows = store.read(
        f"""
        MATCH (f:Function {{repo: $repo}})
        WHERE f.summary_json IS NOT NULL AND f.summary_hash = f.body_hash
        OPTIONAL MATCH p = (f)-[r:CALLS*1..{int(hops)}]->(callee:Function)
        WHERE all(rel IN r WHERE rel.strategy = 'bytecode'
                        OR rel.strategy STARTS WITH 'receiver_type'
                        OR rel.strategy STARTS WITH 'same_')
        WITH f, collect(DISTINCT {{id: callee.id, fqn: callee.fqn,
                                   depth: length(p)}}) AS callees
        RETURN f.id AS id, f.fqn AS fqn, f.file AS file, f.start_line AS line,
               [c IN callees WHERE c.id IS NOT NULL] AS callees
        ORDER BY f.fqn
        """,
        repo=repo,
    )
    return [dict(r) for r in rows]


def _render(target: dict, summaries: dict[str, dict],
            known: dict[str, list[str]] | None = None) -> str | None:
    """Target + neighbourhood as prompt text, or None if the target has no summary."""
    ts = summaries.get(target["id"])
    if not ts:
        return None

    def block(fqn: str, s: dict, indent: str = "") -> str:
        db = s.get("db") or {}
        flags = []
        if db.get("acquires"):
            flags.append("acquires-db")
        if db.get("releases"):
            flags.append("releases")
        if db.get("acquires") and not db.get("released_in_finally"):
            flags.append("release-NOT-guaranteed")
        if db.get("resource_types"):
            flags.append("resources:" + "/".join(db["resource_types"]))
        # The returns line matters most here: a callee that RETURNS a Connection
        # transfers ownership to the caller, and that transfer is the whole class of
        # bug this pass exists to find.
        return (f"{indent}{fqn}\n"
                f"{indent}  does: {s.get('does','')}\n"
                f"{indent}  returns: {s.get('returns','')}\n"
                + (f"{indent}  flags: {', '.join(flags)}\n" if flags else ""))

    lines = ["TARGET FUNCTION:", block(target["fqn"], ts, "  ")]
    prior = (known or {}).get(target["fqn"]) or []
    if prior:
        lines.append("ALREADY REPORTED for this function (do NOT repeat these):")
        lines.extend(f"    - {p}" for p in prior[:6])
    by_depth: dict[int, list[str]] = {}
    for c in target.get("callees") or []:
        s = summaries.get(c["id"])
        if s:
            by_depth.setdefault(int(c.get("depth") or 1), []).append(
                block(c["fqn"], s, "    "))
    for depth in sorted(by_depth):
        lines.append(f"\nCALLS (depth {depth}):")
        lines.extend(by_depth[depth])
    return "\n".join(lines)


def _judge(client, batch: list[dict], summaries: dict[str, dict],
           known: dict[str, list[str]] | None = None):
    rendered, kept = [], []
    for target in batch:
        text = _render(target, summaries, known)
        if text:
            rendered.append(f"### NEIGHBOURHOOD {len(kept)}\n{text}")
            kept.append(target)
    if not rendered:
        return [], [], len(batch)

    user = (f"Review the following {len(rendered)} neighbourhood(s). Return one "
            f"verdict per neighbourhood, using `path_index` for the NEIGHBOURHOOD "
            f"number shown.\n\n" + "\n\n".join(rendered))
    results = []
    for attempt in (1, 2):
        res = client.complete(SYSTEM, user, contract.PATH_VERDICT_SCHEMA)
        results.append(res)
        if not res.parsed:
            continue
        try:
            verdicts = contract.validate_verdicts(res.parsed, len(rendered))
        except contract.ValidationError as exc:
            _log.warning("[neighborhood] invalid verdicts (attempt %s): %s", attempt, exc)
            continue
        for v in verdicts:
            idx = int(v.get("path_index") or 0)
            if 0 <= idx < len(kept):
                v["_target"] = kept[idx]
        return verdicts, results, len(batch) - len(kept)
    return [], results, len(batch)


def _to_finding(verdict: dict) -> dict:
    target = verdict.pop("_target", {}) or {}
    return {
        "kind": verdict.get("kind"),
        "severity": verdict.get("severity"),
        "entry": target.get("fqn"),
        "sink": target.get("fqn"),
        "file": target.get("file"),
        "line": target.get("line"),
        "path_ids": [target.get("id")] if target.get("id") else [],
        "path_fqns": [target.get("fqn")] if target.get("fqn") else [],
        "hops": 0,
        "exploitable": bool(verdict.get("exploitable")),
        "is_defect": bool(verdict.get("is_defect")),
        "reasoning": verdict.get("reasoning"),
        "evidence": verdict.get("evidence") or [],
        "need_source_for": verdict.get("need_source_for") or [],
        "source": "neighborhood",
    }


def run_neighborhood(store, repo: str, hops: int = 2, per_batch: int = 3,
                     model: str | None = None,
                     prior_findings: list[dict] | None = None) -> NeighborhoodReport:
    """Judge every summarized function against its callee closure.

    ``prior_findings`` are shown per function as ALREADY REPORTED so this pass
    returns only what the earlier passes could not see, instead of duplicates
    that cost adjudication and are discarded at the end."""
    model = model or config.summarizer_model()
    rep = NeighborhoodReport()
    t0 = time.monotonic()

    targets = build(store, repo, hops)
    rep.functions_considered = len(targets)
    # A function with no trusted callees has no neighbourhood beyond itself, and Pass
    # A already judged that body alone. Sending it here would re-ask the same question
    # at the same evidence and produce duplicates, not coverage.
    work = [t for t in targets if t.get("callees")]
    rep.skipped_no_callees = len(targets) - len(work)
    if not work:
        rep.seconds = time.monotonic() - t0
        _log.info("[neighborhood] nothing to judge")
        return rep

    ids = {t["id"] for t in work}
    for t in work:
        ids.update(c["id"] for c in t["callees"] if c.get("id"))
    summaries = astore.load_summaries(store, sorted(ids))
    rep.skipped_no_summary = len(ids) - len(summaries)
    rep.neighborhoods_sent = len(work)

    # Prior findings indexed by the function they blame, so each target sees only
    # what is already known about IT rather than a wall of unrelated findings.
    known: dict[str, list[str]] = {}
    for f in prior_findings or []:
        key = f.get("sink") or f.get("entry") or ""
        if key:
            known.setdefault(key, []).append(
                f"{f.get('kind')}: {(f.get('reasoning') or '')[:160]}")
    rep.targets_with_prior = sum(1 for t in work if t["fqn"] in known)

    batches = [work[i:i + per_batch] for i in range(0, len(work), per_batch)]
    _log.info("[neighborhood] %s function(s) in %s batch(es), depth<=%s; model=%s",
              len(work), len(batches), hops, model)

    client = get_client(model, pass_name="neighborhood")
    with ThreadPoolExecutor(max_workers=config.llm_workers()) as pool:
        futures = [pool.submit(_judge, client, b, summaries, known) for b in batches]
        for fut in as_completed(futures):
            try:
                verdicts, results, _skipped = fut.result()
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(str(exc))
                continue
            rep.calls_made += len(results)
            for res in results:
                rep.input_tokens += res.input_tokens
                rep.output_tokens += res.output_tokens
            if not verdicts:
                rep.batches_rejected += 1
                continue
            for v in verdicts:
                if v.get("need_source_for"):
                    rep.needs_expansion += 1
                if v.get("is_defect") or v.get("exploitable") or v.get("need_source_for"):
                    rep.defects += 1 if v.get("is_defect") else 0
                    rep.findings.append(_to_finding(v))

    rep.seconds = time.monotonic() - t0
    _log.info("[neighborhood] done: %s", rep.summary())
    return rep

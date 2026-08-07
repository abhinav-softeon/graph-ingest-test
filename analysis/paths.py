"""Path enumeration inside the reachability universe.

THIS is the combinatorial part, which is why it runs second
Reachability (reach.py) needed no depth bound because it is a closure. Enumeration
does, because the number of paths grows as branching^depth — at the measured 5.34
branching, depth 5 is already ~4,300 endpoints per root. The bound is affordable
here only because reach.py has already cut the graph to functions that actually lie
on an entry->sink path.

HUB EXCLUSION IS NOT AN OPTIMIZATION, IT IS CORRECTNESS OF THE OUTPUT
`STKGeneral#nullCheck` has 33,373 callers and `getStringArray` 16,408. Left in,
they appear on an enormous number of paths and dominate the batch that gets sent to
a model — burning the budget on chains whose middle hop carries no security
meaning. They are excluded from ENUMERATION only; they stay in the reachability
pass, where they drop out naturally because reaching `nullCheck` does not reach a
sink.

TRUSTED EDGES ONLY, AND A PATH IS ONLY AS GOOD AS ITS WEAKEST EDGE
One bare-name hop in the middle makes the whole chain fiction, so every relationship
on an enumerated path must be bytecode/receiver_type/same_*. This is stricter than
filtering the endpoints and it is the point.
"""
from __future__ import annotations

import time

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_TRUSTED_ALL = (
    "all(rel IN relationships(p) WHERE rel.strategy = 'bytecode' "
    "OR rel.strategy STARTS WITH 'receiver_type' "
    "OR rel.strategy STARTS WITH 'same_')"
)

DEFAULT_MAX_DEPTH = 8      # covers controller->service->manager->dao->impl chains
DEFAULT_HUB_CALLERS = 500  # above this a function is plumbing, not a step in a story
# Raised from 2,000 after a measured run hit the cap at EVERY depth, with the
# whole budget consumed by one kind. Because results are ORDER BY hops and then
# truncated, a broad sink set does not cost precision -- it costs RECALL: 359
# Class.forName paths and 183 ResultSet.next paths filled the limit and real
# executeQuery paths were never returned at all. Truncation turns a precision
# problem into a recall problem, so the cap has to sit above the real path count
# rather than act as a sampler.
DEFAULT_PATH_LIMIT = 20000


def find_hubs(store, repo: str, min_callers: int = DEFAULT_HUB_CALLERS) -> list[dict]:
    """Functions called from so many places that they carry no path-specific meaning.

    Measured rather than hardcoded: a name list would go stale, and every codebase
    has different plumbing. Returned with counts so the exclusion is auditable —
    silently dropping nodes from analysis is exactly the kind of invisible cap that
    makes a report read as complete when it is not.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})<-[r:CALLS]-()
        WITH f, count(r) AS callers
        WHERE callers >= $min_callers
        RETURN f.id AS id, f.fqn AS fqn, callers
        ORDER BY callers DESC
        """,
        repo=repo, min_callers=min_callers,
    )
    hubs = [dict(r) for r in rows]
    if hubs:
        _log.info("[paths] excluding %s hub(s) from enumeration, top: %s",
                  len(hubs), ", ".join(f"{h['fqn']}({h['callers']})" for h in hubs[:5]))
    return hubs


def sink_paths(store, repo: str, sink_kinds: list[str] | None = None,
               max_depth: int = DEFAULT_MAX_DEPTH, limit: int = DEFAULT_PATH_LIMIT,
               hub_ids: list[str] | None = None,
               min_depth: int = 0,
               from_taint_source: bool = False) -> list[dict]:
    """Entry-point -> sink paths within the universe.

    Anchored at BOTH ends — starts at a `from_entry` function, ends at one with a
    `CALLS_EXTERNAL` to a dangerous kind — so every returned path is a complete
    story about untrusted data reaching a dangerous operation, not an arbitrary
    call chain.

    `limit` is applied in Cypher and reported by the caller when hit: a truncated
    path set that looks complete is the failure mode this whole module is trying to
    avoid.

    ``from_taint_source`` narrows the start of every path from "reachable from an
    entry point" to "actually reads untrusted data". `from_entry` is a PROPAGATED
    mark -- 106,216 functions on the measured repo -- so without this a path may
    begin at any function downstream of an entry, whether or not tainted data
    enters there. `taint_source` is set at ingest only where the catalog matched
    a real source (request.getParameter, ResultSet.getString): 6,671 functions,
    16x tighter, and it is the actual taint question.

    Nothing is lost by narrowing. A helper that receives tainted data as a
    PARAMETER is still covered, because the path begins at the servlet that read
    it and passes through the helper -- only paths whose origin never touched
    untrusted input disappear, and those were never taint findings.

    ``min_depth`` is part of the PATTERN, not a filter on the results, and that
    distinction is the whole point. Results are ordered by hops ascending, so on a
    JSP-heavy codebase the limit is consumed entirely by 0-hop paths -- an entry
    function that is itself the sink -- before any cross-function chain is
    reached. Measured: 2,000 of 2,000 returned paths were 0-hop, and filtering
    them out afterwards left nothing, because the multi-hop ones were never
    fetched. Setting min_depth=1 makes Neo4j skip those expansions entirely
    instead of ranking them first.
    """
    t0 = time.monotonic()
    hub_ids = hub_ids or []
    # Default to the dangerous set rather than "any External kind at all".
    #
    # Without this the sink end is only required to HAVE some CALLS_EXTERNAL
    # edge, so a function whose only external call is Integer.parseInt (a
    # catalogued SANITIZER) or Connection.close (a release) qualifies as a sink.
    # Measured on a real run: the top "sinks" included Integer.valueOf (72
    # paths), Integer.parseInt (60), Boolean.valueOf (51) and
    # DbManager.getConnection/Connection.close (134), and the returned kinds
    # lists contained taint_sanitizer and taint_source. Those are not sinks, and
    # every path ending at one is a false lead handed to the model.
    #
    # reach.DANGEROUS_KINDS is the same set the backward pass seeds from, so the
    # two ends of the analysis now agree on what "dangerous" means.
    from .reach import DANGEROUS_KINDS
    sink_kinds = sink_kinds or DANGEROUS_KINDS
    kinds_clause = "AND x.kind IN $kinds" if sink_kinds else ""
    taint_clause = ", taint_source: true" if from_taint_source else ""
    rows = store.read(
        f"""
        MATCH (entry:Function {{repo: $repo, from_entry: true{taint_clause}}})
        MATCH p = (entry)-[:CALLS*{int(min_depth)}..{int(max_depth)}]->(sink:Function)
        WHERE sink.reaches_sink = true
          AND {_TRUSTED_ALL}
          AND none(n IN nodes(p) WHERE n.id IN $hub_ids)
          AND all(n IN nodes(p) WHERE n.reaches_sink = true)
        MATCH (sink)-[:CALLS_EXTERNAL]->(x:External)
        WHERE 1=1 {kinds_clause}
        WITH p, entry, sink, collect(DISTINCT x.kind) AS sink_kinds,
             collect(DISTINCT x.name) AS sink_names
        RETURN [n IN nodes(p) | n.id] AS ids,
               [n IN nodes(p) | n.fqn] AS fqns,
               entry.fqn AS entry_fqn, entry.file AS entry_file,
               sink.fqn AS sink_fqn, sink.file AS sink_file,
               sink.start_line AS sink_line,
               sink_kinds, sink_names, length(p) AS hops,
               // Per-hop provenance. A path is only as trustworthy as its
               // WEAKEST edge: an all-bytecode chain is what javac resolved,
               // while one `receiver_type_hint+arity` hop makes the whole chain
               // a guess. Without these a consumer cannot tell the two apart and
               // has to treat 2,000 paths as equally credible, which wastes the
               // model's time on the ones least likely to be real.
               [r IN relationships(p) | r.strategy] AS strategies,
               [r IN relationships(p) | r.confidence] AS confidences,
               // Any catalogued sanitizer applied anywhere along the chain.
               // Not proof the path is safe -- it does not say the sanitizer was
               // applied to THIS value -- but a strong demotion signal, and the
               // single biggest false-positive class while the catalog has few
               // sanitizer entries.
               any(n IN nodes(p) WHERE coalesce(n.taint_sanitizer, false)) AS has_sanitizer
        ORDER BY hops, entry_fqn
        LIMIT $limit
        """,
        repo=repo, kinds=sink_kinds, hub_ids=hub_ids, limit=limit,
    )
    out = [dict(r) for r in rows]
    if len(out) >= limit:
        _log.warning(
            "[paths] hit the %s-path limit — the set is TRUNCATED, not complete. "
            "Narrow sink_kinds or lower max_depth rather than treating this as full "
            "coverage.", limit,
        )
    _log.info("[paths] %s path(s), depth %s..%s, entries=%s, in %.1fs",
              len(out), min_depth, max_depth,
              "taint_source" if from_taint_source else "from_entry",
              time.monotonic() - t0)
    return out


def leak_paths(store, repo: str, max_depth: int = DEFAULT_MAX_DEPTH,
               limit: int = 1000, hub_ids: list[str] | None = None) -> list[dict]:
    """Acquire -> ... paths for leak analysis. A different question, so a different query.

    A leak is not source->sink taint: it is one function acquiring a resource and
    no release on every path out. So this anchors on `db_acquire` and returns the
    call chain BELOW the acquiring function — the frames that could throw between
    acquire and close, which is exactly what Pass B needs to judge whether the
    close is actually guaranteed.
    """
    hub_ids = hub_ids or []
    rows = store.read(
        f"""
        MATCH (acq:Function {{repo: $repo}})-[:CALLS_EXTERNAL]->(:External {{kind: 'db_acquire'}})
        MATCH p = (acq)-[:CALLS*0..{int(max_depth)}]->(tail:Function)
        WHERE {_TRUSTED_ALL}
          AND none(n IN nodes(p) WHERE n.id IN $hub_ids)
        RETURN [n IN nodes(p) | n.id] AS ids,
               [n IN nodes(p) | n.fqn] AS fqns,
               acq.id AS acquire_id, acq.fqn AS acquire_fqn,
               acq.file AS acquire_file, acq.start_line AS acquire_line,
               length(p) AS hops
        ORDER BY acquire_fqn, hops
        LIMIT $limit
        """,
        repo=repo, hub_ids=hub_ids, limit=limit,
    )
    out = [dict(r) for r in rows]
    _log.info("[paths] %s leak path(s)", len(out))
    return out


def dedupe_paths(rows: list[dict]) -> list[dict]:
    """Drop paths that are a prefix-suffix of a longer one to the same sink.

    Variable-length matching returns every sub-path, so one 5-hop chain yields six
    rows ending at the same place. Keeping only the longest per (entry, sink) pair
    means the model sees each distinct story once instead of six times at
    increasing detail.
    """
    best: dict[tuple, dict] = {}
    for row in rows:
        ids = row.get("ids") or []
        if not ids:
            continue
        key = (ids[0], ids[-1])
        prev = best.get(key)
        if prev is None or len(ids) > len(prev.get("ids") or []):
            best[key] = row
    out = sorted(best.values(), key=lambda r: (r.get("hops") or 0, r.get("entry_fqn") or ""))
    if len(out) < len(rows):
        _log.info("[paths] deduped %s -> %s (dropped sub-paths of longer chains)",
                  len(rows), len(out))
    return out


def batch_paths(rows: list[dict], per_batch: int = 3) -> list[list[dict]]:
    """Group paths for Pass B, keeping paths that SHARE a sink together.

    Deliberate: paths converging on one sink are usually the same underlying
    question, so batching them lets the model judge the sink once and reason about
    which entry points actually reach it — better answers than the same paths split
    across unrelated calls, and it also maximizes summary reuse within a call.
    """
    by_sink: dict[str, list[dict]] = {}
    for row in rows:
        by_sink.setdefault(row.get("sink_fqn") or row.get("acquire_fqn") or "", []).append(row)
    batches: list[list[dict]] = []
    for group in by_sink.values():
        for i in range(0, len(group), per_batch):
            batches.append(group[i:i + per_batch])
    return batches

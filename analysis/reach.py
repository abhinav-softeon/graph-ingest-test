"""Reachability marking — the deterministic step between Pass A and Pass B.

WHY THIS EXISTS INSTEAD OF A HOP LIMIT
"Expand N hops and hope" misses end-to-end paths by construction. Real enterprise
Java depth from an entry point to JDBC is controller -> service -> manager -> dao
-> execute, and with *Impl delegation it routinely runs to 6-8. Any fixed N is
either too small (lost recall) or explodes (5.34 branching means ~4,300 functions
at 5 hops).

Reachability is the right primitive because it needs NO depth bound. It is a
transitive closure, computed by iterating one hop at a time to a fixpoint — linear
in edges, complete, and it answers exactly the question that matters: *can* this
function reach a sink at all. Path ENUMERATION is the combinatorial part, and it
runs afterwards, inside the far smaller subgraph this pass identifies.

TWO DIRECTIONS, AND THE INTERSECTION IS THE ANSWER
  reaches_sink  — backward from dangerous External nodes and from Pass A's own
                  `touches` flags. The second seed set matters: it catches sinks
                  the classifier does not know about, and a disagreement between
                  the two is a bug report on the sink catalog.
  from_entry    — forward from HTTP/JAX-WS handlers. Without entry points there is
                  nothing to walk FROM, and the analysis silently returns nothing.

Their intersection is every function lying on some source->sink path. No hop guess,
nothing missed.

TRUSTED EDGES ONLY
Traversal is restricted to bytecode/receiver_type/same_* strategies. Bare-name
matches sit near 5% precision, and a reachability conclusion drawn through one is
worthless — worse than absent, because it looks like a finding.
"""
from __future__ import annotations

import time

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Dangerous External kinds. db_release is deliberately NOT here — closing a
# connection is not a sink, and seeding from it would mark most of the repo.
DANGEROUS_KINDS = ["db_execute", "db_other", "exec", "file_write",
                   "deserialize", "response", "reflection"]

# Sink kinds Pass A can report (contract.TOUCHES minus 'none'), used as the second
# seed set so the LLM's reading supplements the classifier's vocabulary.
SUMMARY_SINKS = ["sql", "exec", "file", "deserialize", "response", "reflection"]

# Annotations that mark an externally-reachable entry point. JAX-WS first: the
# measured repo's entry surface is com.softeon.scm.sei.impl.*, not Spring MVC.
ENTRY_ANNOTATIONS = [
    "WebService", "WebMethod", "Path", "GET", "POST", "PUT", "DELETE",
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
    "RestController", "Controller",
]

# CWE category -> External kind, mirroring external_api._CATEGORY_TO_KIND.
# Duplicated as a plain dict because this is passed to Cypher as a parameter and
# reach.py must not import graph_core just for a lookup table. Anything unmapped
# falls back to the generic taint_sink.
CATEGORY_TO_KIND = {
    "CWE-78/command-injection": "exec",
    "CWE-94/code-injection": "exec",
    "CWE-22/path-traversal": "file_write",
    "CWE-502/unsafe-deserialization": "deserialize",
    "CWE-79/xss": "response",
    "CWE-113/response-splitting": "response",
    "CWE-89/sql-injection": "db_execute",
}

_TRUSTED = (
    "(r.strategy = 'bytecode' OR r.strategy STARTS WITH 'receiver_type' "
    "OR r.strategy STARTS WITH 'same_')"
)

_MAX_ITERATIONS = 50  # runaway guard; a real repo converges in well under 20


def clear_marks(store, repo: str) -> None:
    """Reset marks. Always run before re-marking — a stale `reaches_sink` from a
    previous sink catalog would otherwise be indistinguishable from a fresh one.

    Covers the ANALYSIS-time marks only. The ingest-time taint properties
    (taint_categories / taint_source / taint_sites) are deliberately left alone:
    they are rewritten wholesale by each ingest and are not derived from the sink
    catalog being swapped here."""
    store._run(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.reaches_sink IS NOT NULL OR f.from_entry IS NOT NULL
           OR f.reaches_sink_kinds IS NOT NULL
        SET f.reaches_sink = NULL, f.from_entry = NULL, f.sink_kinds = NULL,
            f.reaches_sink_kinds = NULL
        """,
        repo=repo,
    )


def _count(store, query: str, **params) -> int:
    rows = store.read(query, **params)
    return int(rows[0]["n"]) if rows else 0


def mark_reaches_sink(store, repo: str, kinds: list[str] | None = None,
                      use_summaries: bool = True) -> dict:
    """Mark every function that can reach a dangerous operation. Backward, to fixpoint.

    Seeded from two independent sources so neither is a single point of failure:
    classified `External` nodes, and Pass A's `touches` array. Returns per-stage
    counts including the seed overlap, because a large classifier-only or
    summary-only seed set tells you which side has a gap.
    """
    kinds = kinds or DANGEROUS_KINDS
    t0 = time.monotonic()

    # Seed 1 — the classifier. Also records WHICH kinds, so Pass B can pick seeds
    # per vulnerability class instead of re-deriving them.
    graph_seeds = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})-[:CALLS_EXTERNAL]->(x:External)
        WHERE x.kind IN $kinds
        WITH f, collect(DISTINCT x.kind) AS ks
        SET f.reaches_sink = true,
            f.sink_kinds = ks
        RETURN count(f) AS n
        """,
        repo=repo, kinds=kinds,
    )

    # Seed 2 — what Pass A actually read. Catches sinks the catalog misses; the
    # summary is only trusted when it is fresh for the current body.
    summary_seeds = 0
    if use_summaries:
        summary_seeds = _count(
            store,
            """
            MATCH (f:Function {repo: $repo})
            WHERE f.summary_json IS NOT NULL
              AND f.summary_hash = f.body_hash
              AND any(k IN $sinks WHERE f.summary_json CONTAINS ('"' + k + '"'))
              AND f.reaches_sink IS NULL
            SET f.reaches_sink = true
            RETURN count(f) AS n
            """,
            repo=repo, sinks=SUMMARY_SINKS,
        )

    # Seed 3 — the catalog's own ingest-time marking. Sets sink_kinds too, NOT
    # just the boolean: mark_reaches_sink_kinds seeds from sink_kinds, so a
    # function found only here would propagate no kinds and the kinds pass would
    # cover a strictly smaller set than the boolean one. Measured on a real run
    # before this line existed: 84,167 marked vs 84,024 with kinds, a silent
    # 143-function gap.
    #
    # taint_categories are CWE labels and sink_kinds is External-kind vocabulary,
    # so they are translated rather than mixed — the same two-vocabularies-in-one-
    # property collision that forced the taint_categories rename. Seed 1 above only sees a
    # function whose sink produced a CALLS_EXTERNAL EDGE; this sees every
    # function the catalog recognised a sink in, including the heuristic/JSP path
    # where marks are written straight to the node. Deterministic, so unlike
    # Seed 2 it does not depend on a model having read the function.
    catalog_seeds = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.taint_categories IS NOT NULL AND size(f.taint_categories) > 0
          AND f.reaches_sink IS NULL
        SET f.reaches_sink = true,
            f.sink_kinds = [c IN f.taint_categories
                            | coalesce($catmap[c], 'taint_sink')]
        RETURN count(f) AS n
        """,
        repo=repo, catmap=CATEGORY_TO_KIND,
    )

    # Fixpoint: one hop backward per iteration until nothing new is marked.
    iterations, total_propagated = 0, 0
    while iterations < _MAX_ITERATIONS:
        newly = _count(
            store,
            f"""
            MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
            WHERE callee.reaches_sink = true
              AND caller.reaches_sink IS NULL
              AND {_TRUSTED}
            WITH DISTINCT caller
            SET caller.reaches_sink = true
            RETURN count(caller) AS n
            """,
            repo=repo,
        )
        iterations += 1
        total_propagated += newly
        if newly == 0:
            break
    else:
        _log.warning("[reach] reaches_sink hit the %s-iteration guard — not converged",
                     _MAX_ITERATIONS)

    marked = _count(
        store,
        "MATCH (f:Function {repo: $repo}) WHERE f.reaches_sink = true RETURN count(f) AS n",
        repo=repo,
    )
    out = {"graph_seeds": graph_seeds, "summary_seeds": summary_seeds,
           "catalog_seeds": catalog_seeds,
           "propagated": total_propagated, "total_marked": marked,
           "iterations": iterations, "seconds": round(time.monotonic() - t0, 2)}
    _log.info("[reach] reaches_sink: %s", out)
    return out


def mark_reaches_sink_kinds(store, repo: str) -> dict:
    """Propagate WHICH sink classes each function can reach, not merely whether.

    `mark_reaches_sink` above answers a boolean, and Seed 1 records `sink_kinds`
    only on the functions that call an External sink DIRECTLY. Everything marked
    by the backward fixpoint gets `reaches_sink = true` and nothing else — so a
    caller five hops from a sink cannot be told apart from one that is five hops
    from a `response` write. That matters when a function fans out: if F calls A,
    B and C and only B leads anywhere dangerous, a boolean says "F reaches a
    sink" and gives a path walker no reason to prefer B, so it explores all
    three.

    Run AFTER mark_reaches_sink. Seeds from the `sink_kinds` that seed set
    already wrote, then unions callee sets backward to a fixpoint.

    WHY THIS NEEDS ITS OWN FIXPOINT rather than riding the boolean one: that loop
    marks each caller exactly once (`caller.reaches_sink IS NULL`), which is what
    makes it converge. Kind sets do not work that way — a caller can be marked in
    iteration 1 through a callee whose own kind set is still incomplete, and grow
    again in iteration 3. Merging kinds inside that loop would silently truncate.

    Termination: kind sets only ever GROW and are bounded by the number of
    distinct kinds, and an iteration that grows nothing returns 0 and stops. So
    it converges for the same reason the boolean version does, one level up.

    NOT VERIFIED AGAINST A LIVE DATABASE — written without a Neo4j to run it on.
    The Cypher is straightforward but unexecuted; treat the first real run as the
    test, and check `iterations` is well under the guard.
    """
    t0 = time.monotonic()
    # Seed: whatever Seed 1 of mark_reaches_sink already recorded.
    seeded = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.sink_kinds IS NOT NULL AND size(f.sink_kinds) > 0
          AND f.reaches_sink_kinds IS NULL
        SET f.reaches_sink_kinds = f.sink_kinds
        RETURN count(f) AS n
        """,
        repo=repo,
    )

    iterations, total_grown = 0, 0
    while iterations < _MAX_ITERATIONS:
        grown = _count(
            store,
            f"""
            MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
            WHERE callee.reaches_sink_kinds IS NOT NULL
              AND {_TRUSTED}
            WITH caller, collect(DISTINCT callee.reaches_sink_kinds) AS lists
            WITH caller,
                 reduce(acc = coalesce(caller.reaches_sink_kinds, []), l IN lists |
                        acc + [k IN l WHERE NOT k IN acc]) AS merged
            WHERE size(merged) > size(coalesce(caller.reaches_sink_kinds, []))
            SET caller.reaches_sink_kinds = merged
            RETURN count(caller) AS n
            """,
            repo=repo,
        )
        iterations += 1
        total_grown += grown
        if grown == 0:
            break
    else:
        _log.warning(
            "[reach] reaches_sink_kinds hit the %s-iteration guard — not "
            "converged; the sets below are a lower bound, not the answer",
            _MAX_ITERATIONS)

    marked = _count(
        store,
        "MATCH (f:Function {repo: $repo}) WHERE f.reaches_sink_kinds IS NOT NULL "
        "RETURN count(f) AS n",
        repo=repo,
    )
    out = {"seeded": seeded, "grown": total_grown, "total_marked": marked,
           "iterations": iterations, "seconds": round(time.monotonic() - t0, 2)}
    _log.info("[reach] reaches_sink_kinds: %s", out)
    return out


def mark_from_entry(store, repo: str, annotations: list[str] | None = None) -> dict:
    """Mark every function reachable FROM an entry point. Forward, to fixpoint.

    Three seed sources, because entry-point detection is the most repo-specific
    part of this and a single mechanism silently yields zero:
      * ANNOTATED_WITH -> @WebService/@WebMethod/@RequestMapping/...
      * EXPOSES -> Endpoint (resolved routes)
      * JSP _jspService — in the measured repo JSPs are themselves the entry
        surface and open connections inline, so they are entry points in fact.

    Zero seeds here is the failure that produces an empty analysis with no error,
    so it is logged loudly rather than returned quietly.
    """
    annotations = annotations or ENTRY_ANNOTATIONS
    t0 = time.monotonic()

    annotated = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})-[:ANNOTATED_WITH]->(a:Annotation)
        WHERE a.name IN $annos
        SET f.from_entry = true
        RETURN count(f) AS n
        """,
        repo=repo, annos=annotations,
    )
    exposed = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})-[:EXPOSES]->(:Endpoint)
        WHERE f.from_entry IS NULL
        SET f.from_entry = true
        RETURN count(f) AS n
        """,
        repo=repo,
    )
    jsp = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.lang = 'jsp' AND f.name = '_jspService' AND f.from_entry IS NULL
        SET f.from_entry = true
        RETURN count(f) AS n
        """,
        repo=repo,
    )

    # Seed 4 — the DETERMINISTIC entry surface: any function that pulls in
    # untrusted data is, by definition, where untrusted data enters.
    #
    # This is the seed a legacy servlet app actually needs. Its entry points are
    # `service()` / `doGet()` on HttpServlet subclasses, which carry no
    # annotation, expose no resolved route, and are not JSPs — so all three
    # structural seeds above return nothing for them. Measured: the graph holds
    # 16 Annotation nodes in total, against 93k HttpServletRequest calls.
    #
    # Set at INGEST from the vulnerability catalog (Node.taint_source), so unlike
    # the Pass A seed below it needs no model and no summary to be fresh.
    catalog_entry = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.taint_source = true AND f.from_entry IS NULL
        SET f.from_entry = true
        RETURN count(f) AS n
        """,
        repo=repo,
    )

    # Seed 5 — what Pass A read in the code. UNION with the three structural seeds
    # above, never an intersection: the model can only ADD entry points here, so a
    # false negative costs nothing that the annotations did not already cover, while
    # a true positive rescues a repo whose entry convention is not in
    # ENTRY_ANNOTATIONS. That case is not hypothetical — the sink side has had a
    # summary seed since the start (SUMMARY_SINKS below), and this side did not,
    # which meant an unrecognised entry convention produced an empty universe with
    # nothing but a log line to explain it.
    llm = _count(
        store,
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.sig_entry = true AND f.from_entry IS NULL
        SET f.from_entry = true
        RETURN count(f) AS n
        """,
        repo=repo,
    )

    seeds = annotated + exposed + jsp + catalog_entry + llm
    if seeds == 0:
        _log.warning(
            "[reach] NO ENTRY POINTS FOUND — the forward pass has nothing to walk "
            "from and the universe will be empty. Check ANNOTATED_WITH edges and "
            "ENTRY_ANNOTATIONS; and if this is a servlet app, check that ingest "
            "wrote Function.taint_source (needs GRAPH_CATALOG_EXTERNAL on and a "
            "cache free of pre-taint-field Node pickles)."
        )

    iterations, total_propagated = 0, 0
    while iterations < _MAX_ITERATIONS:
        newly = _count(
            store,
            f"""
            MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
            WHERE caller.from_entry = true
              AND callee.from_entry IS NULL
              AND {_TRUSTED}
            WITH DISTINCT callee
            SET callee.from_entry = true
            RETURN count(callee) AS n
            """,
            repo=repo,
        )
        iterations += 1
        total_propagated += newly
        if newly == 0:
            break
    else:
        _log.warning("[reach] from_entry hit the %s-iteration guard — not converged",
                     _MAX_ITERATIONS)

    marked = _count(
        store,
        "MATCH (f:Function {repo: $repo}) WHERE f.from_entry = true RETURN count(f) AS n",
        repo=repo,
    )
    # Seed counts are reported per source, not summed, because their RATIO is the
    # diagnostic: llm_seeds dwarfing annotated_seeds means this repo's entry
    # convention is missing from ENTRY_ANNOTATIONS and should be added there, where
    # it costs nothing, rather than left to the model to rediscover every run.
    out = {"annotated_seeds": annotated, "endpoint_seeds": exposed, "jsp_seeds": jsp,
           "catalog_entry_seeds": catalog_entry,
           "llm_seeds": llm,
           "propagated": total_propagated, "total_marked": marked,
           "iterations": iterations, "seconds": round(time.monotonic() - t0, 2)}
    _log.info("[reach] from_entry: %s", out)
    return out


def universe(store, repo: str) -> dict:
    """The intersection — every function on some entry->sink path.

    Reported alongside the whole-repo total because the ratio is the number that
    tells you whether Pass B is cheap. A universe near 100% usually means the sink
    seeds are too broad (most often GRAPH_EXTERNAL_ALL_CALLS left on, so
    `StringBuilder.append` counts as a sink and pruning does nothing).
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        RETURN count(f) AS total,
               sum(CASE WHEN f.reaches_sink = true THEN 1 ELSE 0 END) AS reaches_sink,
               sum(CASE WHEN f.from_entry = true THEN 1 ELSE 0 END) AS from_entry,
               sum(CASE WHEN f.reaches_sink = true AND f.from_entry = true
                        THEN 1 ELSE 0 END) AS both
        """,
        repo=repo,
    )
    out = dict(rows[0]) if rows else {}
    total = out.get("total") or 0
    out["universe_fraction"] = round((out.get("both") or 0) / total, 4) if total else 0.0
    _log.info("[reach] universe: %s", out)
    return out


def mark_all(store, repo: str, kinds: list[str] | None = None) -> dict:
    """clear -> backward -> kinds -> forward -> report. The whole pre-Pass-B step.

    NOTE ON WHEN THIS RUNS: this is the ANALYSIS step, invoked from
    analysis/pipeline.py during a REVIEW — not during a graph build. So a
    freshly built graph has `taint_source`/`taint_categories` (written at ingest)
    but NO `from_entry`/`reaches_sink`, and the universe is legitimately 0 until
    a review runs. That is not a broken build; it is two different sets of marks.
    """
    clear_marks(store, repo)
    sink = mark_reaches_sink(store, repo, kinds)
    # After the boolean pass, which is what seeds it: a function only gets kinds
    # once Seed 1 has recorded its direct sink_kinds.
    kinds_out = mark_reaches_sink_kinds(store, repo)
    entry = mark_from_entry(store, repo)
    return {"reaches_sink": sink, "reaches_sink_kinds": kinds_out,
            "from_entry": entry, "universe": universe(store, repo)}

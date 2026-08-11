"""Option B — DERIVE-IN-DATABASE (Cypher). PARTIALLY WIRED IN.

An alternative to computing the derived layer (OVERRIDES, polymorphic CALLS)
in Python: write only the BASE edges (extracted + resolved) to Neo4j/Aura,
then compute the derived layer with Cypher *inside the database*. This keeps
the ingest client's RAM ~0 for the derive step — the DB holds and aggregates
the 100M+ edges, not your process.

``synthesize_polymorphic_calls_cypher`` below IS imported and called by
pipeline.index_repo after the final edge write. ``derive_overrides_cypher`` is
still a dormant template — OVERRIDES is cheap in Python (it reads only the
structural edges, which stay in RAM by design) so there was no reason to move
it. Read every caveat before wiring anything else in.

Works identically on local Neo4j and AuraDB (Cypher is portable; batching uses
native `CALL { } IN TRANSACTIONS`, no APOC required).

KNOWN SEMANTIC DIFFERENCES vs the Python derive (why this is "reference", not
"equivalent"):
  * synthesize_polymorphic_calls_cypher IS now wired into the pipeline (it
    replaced the Python passes). It deliberately drops the Python version's
    arbitrary top-25 fan-out guard, so its edge set is a superset on wide
    hierarchies — an intentional divergence, rebaselined once.
  * (fan_in/fan_out was removed entirely, not migrated — MEMORY_ARCHITECTURE_
    PLAN.md item #8 confirmed zero downstream consumers, so there was no need
    to compute it anywhere, Python or Cypher. `attach_call_metrics_cypher`,
    which used to live in this file, is gone along with pipeline.
    _attach_call_metrics.)

Each function takes a GraphStore (see graph_core.store) and a repo namespace,
and runs server-side. Call them AFTER the base edges are in the DB.
"""
from __future__ import annotations

# from .store import GraphStore  # uncomment when wiring in


def derive_overrides_cypher(store, repo: str) -> None:
    """OVERRIDES edges: a method overrides an ancestor method of the same name +
    arity, following EXTENDS/IMPLEMENTS. Server-side, batched.

    Simplified: single-level name+arity match per super-edge; the Python version
    walks the full ancestor BFS. Extend the pattern (`*1..`) if you need the
    transitive hierarchy."""
    store._run(
        """
        MATCH (sub:CodeNode {repo:$repo, label:'Class'})-[:EXTENDS|IMPLEMENTS]->(sup:CodeNode {label:'Class'})
        MATCH (sub)-[:CONTAINS]->(m:CodeNode {label:'Function', kind:'method'})
        MATCH (sup)-[:CONTAINS]->(a:CodeNode {label:'Function', kind:'method'})
        WHERE m.name = a.name AND m.param_count = a.param_count AND m <> a
        CALL (m, a) {
            MERGE (m)-[r:OVERRIDES]->(a)
            ON CREATE SET r.confidence='INFERRED', r.origin='DERIVED',
                          r.extractor='cypher', r.strategy='hierarchy'
        } IN TRANSACTIONS OF 2000 ROWS
        """,
        repo=repo,
    )


def synthesize_polymorphic_calls_cypher(store, repo: str) -> int:
    """Make callers of an ancestor method visible as callers of each concrete
    override (child -> ancestor -> ancestor's callers), server-side. Returns the
    number of synthetic CALLS relationships created.

    WIRED IN — this is no longer reference-only. ``pipeline.index_repo`` calls it
    after the final edge write (it needs every CALLS and every OVERRIDES to be
    durable first), replacing the Python ``_synthesize_polymorphic_calls`` /
    ``streaming_polymorphic_calls`` passes. Those were the last thing in derive
    that scanned the whole CALLS bulk.

    ``ON CREATE`` means an already-existing real CALLS is never clobbered — the
    database does the dedup the Python ``existing_calls`` set used to do.

    Deliberately UNCAPPED. The Python version took only the first 25 callers per
    ancestor (``_POLY_FANOUT_GUARD``), an arbitrary limit whose selection also
    depended on Python list-append order — which is why it had no faithful Cypher
    equivalent. Dropping it makes the result complete and order-independent
    instead of arbitrary; on a very wide hierarchy it can create more edges than
    the capped version did.

    ``evidence_file`` is intentionally NOT propagated: nothing reads it back and
    it is no longer written on base edges either. ``evidence_line`` is, because
    ``exception_walk.py`` and ``two_agent``'s ``caller_evidence_lines`` read it.
    """
    def _count() -> int:
        rows = store.read(
            "MATCH (:CodeNode {repo:$repo})-[r:CALLS]->() "
            "WHERE r.strategy='polymorphic_dispatch' RETURN count(r) AS n",
            repo=repo,
        )
        return int(rows[0]["n"]) if rows else 0

    # Counted by difference rather than by RETURN, because the MERGE now runs
    # inside CALL { ... } IN TRANSACTIONS, which cannot also return an aggregate.
    before = _count()
    # BATCHED. This used to be a single implicit transaction, which held every
    # created relationship in transaction state until commit — on a wide
    # hierarchy (27k OVERRIDES fanning out across all callers of each ancestor,
    # and this pass is deliberately uncapped) that exhausts
    # dbms.memory.transaction.total.max and aborts the whole query. Because the
    # caller treats a failure here as fatal, that took the entire finished graph
    # down with it. `derive_overrides_cypher` above was already batched this way;
    # this one was not, and it is the pass with far more fan-out.
    store._run(
        """
        MATCH (child:CodeNode {repo:$repo})-[:OVERRIDES]->(anc:CodeNode)
        MATCH (caller:CodeNode)-[ce:CALLS]->(anc)
        WHERE NOT (caller)-[:CALLS]->(child)
        CALL (caller, child, ce) {
            MERGE (caller)-[r:CALLS]->(child)
            ON CREATE SET r.confidence='AMBIGUOUS', r.origin='DERIVED',
                          r.strategy='polymorphic_dispatch',
                          r.evidence_line=ce.evidence_line
        } IN TRANSACTIONS OF 1000 ROWS
        """,
        repo=repo,
    )
    return max(0, _count() - before)


def mark_inrepo_acquires_cypher(store, repo: str) -> int:
    """Tag every in-repo method that RETURNS a connection as a `db_acquire`.

    THE GAP THIS CLOSES
    external_api classifies an acquire by RETURN TYPE, and that rule is the one
    that matters on a real codebase: measured on this app, 188 in-repo methods
    declare a `Connection` return, spelled `getConnection`, `getDbConn`, `getCon`,
    `getDbConnection` — no naming convention any rule could key on. Meanwhile
    3,235 files call `getConnection`. Connections come from the repo's own pool
    wrappers, not from JDBC directly.

    But that rule only ever fired on the BYTECODE path, which reads the callee's
    descriptor (bytecode_resolver passes `type_name(ret)`). The heuristic path
    calls `classify_call(recv_type, name)` with no return type at all, and it
    cannot do better: `DbManager` is an in-repo class, so the call never reaches
    the external-receiver branch where classification happens. Result: with
    bytecode off, or for any file it did not attribute, a pool wrapper produced NO
    database fact whatsoever.

    WHY CYPHER AND NOT A PYTHON DERIVE PASS
    `return_type` is persisted on Function nodes (models.Node.props) but is NOT in
    SLIM_NODE_FIELDS — from the pre-resolve write onward `all_nodes` is a SlimNode
    list, so no in-process pass can read it without widening that projection and
    paying for it on every node in the graph. The database already has the value.

    Runs AFTER the final write, like the polymorphic pass, and is counted by
    difference for the same reason.
    """
    # Simple names, because java.py stores return_type via simple_type_name.
    # Kept in sync with external_api.CONNECTION_TYPES rather than duplicated.
    from .external_api import CONNECTION_SIMPLE_NAMES, DB_ACQUIRE

    def _count() -> int:
        rows = store.read(
            "MATCH (:CodeNode {repo:$repo})-[r:CALLS_EXTERNAL]->(x:External) "
            "WHERE x.kind=$kind AND r.strategy=$strategy RETURN count(r) AS n",
            repo=repo, kind=DB_ACQUIRE, strategy="inrepo_return_type",
        )
        return int(rows[0]["n"]) if rows else 0

    before = _count()
    # One shared External target per wrapper, keyed on the wrapper's own fqn, so
    # the graph says WHICH acquire this is rather than collapsing all 188 onto one
    # node — reachability seeds from the kind, but a reviewer needs the identity.
    store._run(
        """
        MATCH (f:Function:CodeNode {repo:$repo})
        WHERE f.return_type IN $conn_types
        CALL (f) {
            MERGE (x:External:CodeNode {id: 'acq:' + f.fqn})
            ON CREATE SET x.name = f.name, x.fqn = f.fqn, x.repo = 'external',
                          x.kind = $kind, x.lang = f.lang
            MERGE (f)-[r:CALLS_EXTERNAL]->(x)
            ON CREATE SET r.confidence='EXTRACTED', r.origin='DERIVED',
                          r.extractor='cypher', r.strategy='inrepo_return_type'
        } IN TRANSACTIONS OF 1000 ROWS
        """,
        repo=repo, conn_types=sorted(CONNECTION_SIMPLE_NAMES), kind=DB_ACQUIRE,
    )
    return max(0, _count() - before)


# USES aggregation (component/module) is intentionally omitted: faithfully
# reproducing owner_component() (walk to the nearest Class/File/Endpoint/Module)
# plus the cross_module/intra_module tagging in pure Cypher is a project of its
# own and would not match the Python edge-set hash. If you need module-level
# blast-radius in-DB, start from:
#   MATCH (s)-[:CALLS|READS|WRITES|IMPORTS|...]->(d)
#   MATCH (s)<-[:CONTAINS*]-(sc {label:'Class'})   // nearest owning component
#   MATCH (d)<-[:CONTAINS*]-(dc {label:'Class'})
#   WHERE sc <> dc
#   MERGE (sc)-[:USES]->(dc)
# ...and add the Module layer + boundary tags on top. Treat as a research task.

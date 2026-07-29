"""Option B — DERIVE-IN-DATABASE reference (Cypher).  ⚠️ DORMANT / UNVERIFIED.

An alternative to computing the derived layer (OVERRIDES, polymorphic CALLS)
in Python: write only the BASE edges (extracted + resolved) to Neo4j/Aura,
then compute the derived layer with Cypher *inside the database*. This keeps
the ingest client's RAM ~0 for the derive step — the DB holds and aggregates
the 100M+ edges, not your process.

Nothing in the pipeline imports this module. It is a starting template you can
wire in later (e.g. from index_repo, after a base-edge write) — NOT a validated
drop-in. Read every caveat before trusting a number.

Works identically on local Neo4j and AuraDB (Cypher is portable; batching uses
native `CALL { } IN TRANSACTIONS`, no APOC required).

KNOWN SEMANTIC DIFFERENCES vs the Python derive (why this is "reference", not
"equivalent"):
  * The polymorphic fan-out guard is non-trivial to express faithfully in
    Cypher — the version below is simplified and will not match the Python
    edge-set hash. Validate before use.
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


def synthesize_polymorphic_calls_cypher(store, repo: str) -> None:
    """Make callers of an ancestor method visible as callers of each concrete
    override (child -> ancestor -> ancestor's callers). Uses ON CREATE SET so an
    already-existing real CALLS is never clobbered — the DB does the dedup the
    Python `existing_calls` set did.

    CAVEAT: does not apply the per-ancestor fan-out guard (top-25) the Python
    version uses; on huge hierarchies add a `WITH ... LIMIT` per ancestor."""
    store._run(
        """
        MATCH (child:CodeNode {repo:$repo})-[:OVERRIDES]->(anc:CodeNode)
        MATCH (caller:CodeNode)-[ce:CALLS]->(anc)
        CALL (caller, child, ce) {
            MERGE (caller)-[r:CALLS]->(child)
            ON CREATE SET r.confidence='AMBIGUOUS', r.origin='DERIVED',
                          r.extractor='cypher', r.strategy='polymorphic_dispatch',
                          r.evidence_file=ce.evidence_file, r.evidence_line=ce.evidence_line
        } IN TRANSACTIONS OF 2000 ROWS
        """,
        repo=repo,
    )


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

"""Option B — DERIVE-IN-DATABASE reference (Cypher).  ⚠️ DORMANT / UNVERIFIED.

An alternative to computing the derived layer (fan_in/fan_out, OVERRIDES,
polymorphic CALLS, USES) in Python: write only the BASE edges (extracted +
resolved) to Neo4j/Aura, then compute the derived layer with Cypher *inside the
database*. This keeps the ingest client's RAM ~0 for the derive step — the DB
holds and aggregates the 100M+ edges, not your process.

Nothing in the pipeline imports this module. It is a starting template you can
wire in later (e.g. from index_repo, after a base-edge write) — NOT a validated
drop-in. Read every caveat before trusting a number.

Works identically on local Neo4j and AuraDB (Cypher is portable; batching uses
native `CALL { } IN TRANSACTIONS`, no APOC required).

KNOWN SEMANTIC DIFFERENCES vs the Python derive (why this is "reference", not
"equivalent"):
  * fan_in/fan_out: Cypher `count()` counts MERGE-collapsed relationships, so it
    is the number of DISTINCT callers/callees, NOT call-site multiplicity. The
    Python path counts multiplicity. If you rely on multiplicity, keep fan
    metrics in Python.
  * USES aggregation / owner-component walks and the polymorphic fan-out guard
    are non-trivial to express faithfully in Cypher — the versions below are
    simplified and will not match the Python edge-set hash. Validate before use.

Each function takes a GraphStore (see graph_core.store) and a repo namespace,
and runs server-side. Call them AFTER the base edges are in the DB.
"""
from __future__ import annotations

# from .store import GraphStore  # uncomment when wiring in


def attach_call_metrics_cypher(store, repo: str) -> None:
    """fan_in / fan_out / recursive on Function nodes, computed in the DB.

    CAVEAT: counts distinct relationships (MERGE-collapsed), not call-site
    multiplicity. Batched so it never builds one giant transaction."""
    store._run(
        """
        MATCH (n:CodeNode {repo:$repo})
        WHERE n.label = 'Function'
        CALL (n) {
            OPTIONAL MATCH (n)-[co:CALLS]->()
            OPTIONAL MATCH (n)<-[ci:CALLS]-()
            SET n.fan_out = count(DISTINCT co),
                n.fan_in  = count(DISTINCT ci)
        } IN TRANSACTIONS OF 5000 ROWS
        """,
        repo=repo,
    )
    store._run(
        """
        MATCH (n:CodeNode {repo:$repo})-[:CALLS]->(n)
        CALL (n) { SET n.recursive = true } IN TRANSACTIONS OF 5000 ROWS
        """,
        repo=repo,
    )


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

"""Deterministic graph validation helpers (M7 baseline)."""
from __future__ import annotations

from collections import defaultdict

from .models import Node


REQUIRED_RELATIONS = {
    "CONTAINS",
    "CALLS",
}


class EdgeStats:
    """Running per-type edge counts + dangling tally.

    This exists so ``validate_graph`` never needs the edge list. Its two
    edge-side checks — a per-type histogram and a dangling-endpoint count — were
    the ONLY reason the last derive step had to scan every edge in the graph
    right before the write, which is what kept the full (Slim)Edge list alive
    through derive at ~100M edges. Both are pure accumulations, so they are
    tallied as edges are produced instead: O(1) memory, no extra pass, and the
    numbers are identical because this counts the same multiset the scan did.

    ``add`` is called wherever edges reach their final form (extraction, the
    resolve sink, each derive pass). ``node_ids`` must already contain the
    endpoints of the edges being added — guaranteed by the sink contract, which
    requires a batch's newly-synthesized nodes to be handed over before the
    batch itself (see resolver.resolve's ``edge_sink`` docstring).
    """

    __slots__ = ("rel_counts", "dangling", "total")

    def __init__(self):
        self.rel_counts: dict[str, int] = defaultdict(int)
        self.dangling = 0
        self.total = 0

    def add(self, edges, node_ids: set[str]) -> None:
        rel_counts = self.rel_counts
        dangling = 0
        total = 0
        for e in edges:
            rel_counts[e.type] += 1
            total += 1
            if e.src not in node_ids or e.dst not in node_ids:
                dangling += 1
        self.total += total
        self.dangling += dangling

    def add_count(self, edge_type: str, n: int) -> None:
        """Fold in edges created without passing through Python — currently the
        polymorphic-dispatch CALLS built server-side by Cypher after the write.
        They are real graph edges, so the reported totals must include them."""
        if n:
            self.rel_counts[edge_type] += n
            self.total += n

    def reset(self) -> None:
        """Drop everything counted so far — for the rare path (SCIP) that
        replaces whole languages' edges after the fact and so has to recount
        from the surviving list rather than adjust incrementally."""
        self.rel_counts = defaultdict(int)
        self.dangling = 0
        self.total = 0


def validate_graph(nodes: list[Node], edge_stats: EdgeStats) -> dict:
    """Node-side invariants plus the edge tallies accumulated in ``edge_stats``.

    Takes the stats, not the edges: every edge-side check here is a count, and
    counting as edges are produced is strictly cheaper than scanning them all
    once more at the end (see EdgeStats).
    """
    errors: list[str] = []
    warnings: list[str] = []

    node_ids = [n.id for n in nodes]
    node_id_set = set(node_ids)

    if len(node_ids) != len(node_id_set):
        errors.append("duplicate node ids detected")

    dangling = edge_stats.dangling
    if dangling:
        errors.append(f"dangling edges detected: {dangling}")

    bad_ranges = 0
    for n in nodes:
        if n.start_line and n.end_line and n.end_line < n.start_line:
            bad_ranges += 1
        # A second clause compared end_col < start_col on single-line nodes. It
        # could never fire: the pre-resolve write blanks end_col in RAM, and this
        # runs after it, so `n.end_col and ...` was always falsy. Same class of
        # dead check as the loc/cyclomatic warning removed below.
    if bad_ranges:
        errors.append(f"invalid source ranges detected: {bad_ranges}")

    rel_counts = dict(edge_stats.rel_counts)
    for rel in sorted(REQUIRED_RELATIONS):
        if rel_counts.get(rel, 0) == 0:
            warnings.append(f"required relation has zero edges: {rel}")

    fn_nodes = sum(1 for n in nodes if n.label == "Function")

    # A "function nodes missing core metrics" warning lived here, checking
    # n.loc/n.cyclomatic. It fired for EVERY function regardless of the graph's
    # health: the pre-resolve node write blanks loc/cyclomatic in RAM (they are
    # already durable in Neo4j by then), and this runs after that — so it only
    # ever measured "the blanking ran", never a real defect. Removed rather than
    # left as permanent noise; those metrics are no longer persisted either.

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "nodes": len(nodes),
            "edges": edge_stats.total,
            "functions": fn_nodes,
            "dangling_edges": dangling,
            "invalid_ranges": bad_ranges,
            "relations": rel_counts,
        },
    }

"""Deterministic graph validation helpers (M7 baseline)."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

from .models import Node

_log = logging.getLogger(__name__)

# How many distinct colliding ids to name in the log / error string. A collision
# is usually one systematic cause repeated thousands of times (the same class
# reachable at two paths), so a handful of examples identifies it; dumping all of
# them would bury the rest of the report.
_DUP_SAMPLE = 20


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
        # An id is sha1(repo + kind + fqn) — deliberately path-independent so an
        # incremental re-index patches a moved file in place (see ids.make_id).
        # The consequence is that two source files declaring the SAME package +
        # type collide, and the bare count cannot tell that (benign: one type
        # reachable at two paths, e.g. sources also copied under WEB-INF/classes)
        # apart from a genuine fqn collision between two DIFFERENT types. Only
        # the fqn and the owning files distinguish them, so report both.
        #
        # Neo4j MERGE collapses the duplicates on write, which is why a run with
        # this error still produces a queryable graph. The damage is upstream:
        # the duplicates ride through resolve/derive in RAM, and anything keyed
        # by node id (a per-function CFG/DFG especially) silently merges two
        # distinct bodies into one.
        #
        # Counted only when a duplicate is already known to exist — the Counter
        # is a third ~len(nodes) structure and the healthy path must not pay for
        # it at the pre-write memory peak.
        dupes = {i: c for i, c in Counter(node_ids).items() if c > 1}
        by_id: dict[str, list[Node]] = defaultdict(list)
        for n in nodes:
            if n.id in dupes:
                by_id[n.id].append(n)
        extra = len(node_ids) - len(node_id_set)
        errors.append(
            f"duplicate node ids detected: {len(dupes)} id(s), "
            f"{extra} redundant node(s)"
        )
        for nid, count in sorted(dupes.items(), key=lambda kv: -kv[1])[:_DUP_SAMPLE]:
            group = by_id[nid]
            first = group[0]
            _log.error(
                "[validate] dup id %s x%s label=%s kind=%s fqn=%s files=%s",
                nid, count, first.label, first.kind or "-", first.fqn or "-",
                sorted({n.file for n in group if n.file}) or ["-"],
            )
        if len(dupes) > _DUP_SAMPLE:
            _log.error(
                "[validate] ... and %s more duplicated id(s) not listed",
                len(dupes) - _DUP_SAMPLE,
            )

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

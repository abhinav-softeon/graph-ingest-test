"""End-to-end contract tests for index_repo, driven over the real Java corpus.

Replaces test_lowram_equivalence.py, which compared the GRAPH_LOWRAM_DERIVE path
against the default one. That flag is gone (item #16) — the default path now
retains only structural edges, which is what it existed to achieve — so there is
no second path to compare against. What survived is the part that was never
really about low-RAM: guarding the invariants those tests happened to pin down.

The Java corpus is used because it is the only one with inheritance, interfaces
and overrides; the Python corpus emits no EXTENDS at all.
"""
from __future__ import annotations

import dataclasses
import os
import threading

import pytest

from graph_core import pipeline
from graph_core.models import Edge, Node, SlimEdge, SlimNode

_CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "test_corpora", "java_sample")


class RecordingStore:
    """Folds the write stream into the final state Neo4j would hold: MERGE
    semantics, i.e. last write wins per node id and per (type, src, dst)."""

    def __init__(self):
        self._lock = threading.Lock()   # the streaming writer writes off-thread
        self.nodes: dict = {}
        self.edges: dict = {}
        self.queries: list = []

    def bootstrap(self):
        pass

    def wipe(self, repo):
        with self._lock:
            self.nodes.clear()
            self.edges.clear()

    def delete_files(self, repo, files):
        pass

    def write_nodes(self, nodes, on_batch=None):
        with self._lock:
            for n in nodes:
                self.nodes[n.id] = n

    def write_edges(self, edges, on_batch=None):
        with self._lock:
            for e in edges:
                self.edges[(e.type, e.src, e.dst)] = e

    def write_semantics(self, rows):
        pass

    def read(self, query, **params):
        # Polymorphic dispatch is a Cypher pass now (item #12), so against a
        # fake store it is a no-op. NOTE the coverage gap this implies: the
        # synthetic caller->override edges cannot be asserted from the suite at
        # all — only that the query is issued. Confirming they are correct needs
        # a run against a real Neo4j.
        with self._lock:
            self.queries.append(query)
        return [{"created": 0}]

    def counts(self, repo):
        return (len(self.nodes), len(self.edges))


def _run(**kwargs):
    store = RecordingStore()
    result = pipeline.index_repo(_CORPUS, "contract", store, wipe=True, javac=False, **kwargs)
    return store, result


@pytest.mark.skipif(not os.path.isdir(_CORPUS), reason="java_sample corpus missing")
def test_overrides_derived_from_the_resolved_hierarchy(monkeypatch):
    """The regression guard for the worst bug this project has had: EXTENDS and
    IMPLEMENTS are NOT emitted by the extractors — they only come into existence
    inside resolve(). If the edge sink stops retaining them, _derive_overrides
    sees an empty class hierarchy and silently emits zero OVERRIDES, with no
    error anywhere.

    The polymorphic pass is default-OFF now (config.polymorphic_dispatch_enabled),
    so it is enabled explicitly here: what this test guards is that OVERRIDES
    reaches it, which is only observable when it actually runs."""
    monkeypatch.setenv("GRAPH_POLYMORPHIC_DISPATCH", "true")
    store, _ = _run()
    by_type: dict[str, int] = {}
    for (etype, _src, _dst) in store.edges:
        by_type[etype] = by_type.get(etype, 0) + 1

    assert by_type.get("EXTENDS", 0) > 0, "corpus no longer exercises inheritance"
    assert by_type.get("OVERRIDES", 0) > 0, (
        "zero OVERRIDES — the resolved EXTENDS/IMPLEMENTS edges are not reaching "
        "_derive_overrides (is the sink retaining _RETAINED_EDGE_TYPES?)"
    )
    assert any("polymorphic_dispatch" in q for q in store.queries), (
        "the in-database polymorphic pass was never invoked"
    )


@pytest.mark.skipif(not os.path.isdir(_CORPUS), reason="java_sample corpus missing")
def test_polymorphic_dispatch_is_off_by_default(monkeypatch):
    """The 53.6%-of-all-CALLS pass must stay opt-in. Every one of its edges is
    AMBIGUOUS, and any consumer filtering to strategy='bytecode' excludes them —
    so if this ever flips back to default-on, half the write volume returns for
    rows most queries deliberately ignore."""
    monkeypatch.delenv("GRAPH_POLYMORPHIC_DISPATCH", raising=False)
    store, _ = _run()
    assert not any("polymorphic_dispatch" in q for q in store.queries), (
        "polymorphic dispatch ran without GRAPH_POLYMORPHIC_DISPATCH being set"
    )


@pytest.mark.skipif(not os.path.isdir(_CORPUS), reason="java_sample corpus missing")
def test_bulk_edges_are_not_retained_in_ram():
    """Item #2b: the sink must keep only structural edges. This is the invariant
    the whole edge-memory story rests on, and nothing else would notice if it
    regressed — the graph would still be correct, just gigabytes heavier."""
    seen: dict[str, int] = {"total": 0, "kept": 0}
    real_resolve = pipeline.resolve

    def counting_resolve(nodes, edges, refs, repo, **kw):
        sink = kw.get("edge_sink")
        if sink is not None:
            def wrapped(new_nodes, batch):
                out = sink(new_nodes, batch)
                seen["total"] += len(batch)
                seen["kept"] += len(out)
                return out
            kw["edge_sink"] = wrapped
        return real_resolve(nodes, edges, refs, repo, **kw)

    pipeline.resolve = counting_resolve
    try:
        _run()
    finally:
        pipeline.resolve = real_resolve

    assert seen["total"] > 0, "resolve produced no edges — corpus broken?"
    retained_types = pipeline._RETAINED_EDGE_TYPES
    assert "CALLS" not in retained_types, "CALLS must never be retained"
    # Every retained edge must be structural, so the ratio tracks declarations
    # rather than call sites. The bound is loose on a tiny corpus; what matters
    # is that it cannot silently become "everything".
    assert seen["kept"] < seen["total"], (
        f"the sink retained every edge ({seen['kept']}/{seen['total']}) — the bulk "
        "is accumulating in RAM again"
    )


def test_removed_payload_fields_stay_removed():
    """PASSES edges (item #9) and their payload arrays, plus arg_names (#15),
    were removed after a full-monorepo grep found zero consumers. Guard against
    them creeping back: PASSES cost ~700 B per edge held as a full Edge through
    the derive->write peak, and arg_names cost a per-call-site AST walk during
    extraction for a field that was always empty."""
    from graph_core.schema import EDGE_TYPES

    assert "PASSES" not in EDGE_TYPES

    dead = {"flow_from_param", "flow_to_param", "flow_lines", "const_args", "arg_names"}
    present = {f.name for f in dataclasses.fields(Edge)} & dead
    assert not present, f"removed Edge payload fields are back: {present}"


def test_slim_projections_are_strictly_smaller():
    """The projections are the entire point of items #2b/#14 — if a field creeps
    back onto SlimEdge/SlimNode the saving quietly evaporates."""
    import sys

    e = Edge("CALLS", "a" * 40, "b" * 40, evidence_file="f.py", evidence_line=1)
    n = Node(id="a", label="Function", name="n", fqn="f", repo="r")
    assert sys.getsizeof(e.to_slim()) < sys.getsizeof(e)
    assert sys.getsizeof(n.to_slim()) < sys.getsizeof(n)
    # SlimEdge is identity-only: anything more means a derive pass started
    # reading provenance off an existing edge again.
    assert {f.name for f in dataclasses.fields(SlimEdge)} == {"type", "src", "dst"}
    # SlimNode must stay well under Node; it is what all_nodes holds.
    assert len(dataclasses.fields(SlimNode)) < len(dataclasses.fields(Node)) / 2

"""Equivalence tests for the Option A (low-RAM) derive building blocks.

These prove the streamed variants produce byte-identical results to the proven
in-RAM functions in pipeline.py, on randomized synthetic graphs. This is the
correctness anchor for the low-RAM path (the full pipeline still needs the
golden-oracle run on real data, but these pin the risky rewrites).
"""
from __future__ import annotations

import random
import tempfile

from graph_core.models import Confidence, Edge, Node, Origin
from graph_core.pipeline import _synthesize_polymorphic_calls
from graph_core.lowram_derive import (
    DiskEdgeStore,
    streaming_polymorphic_calls,
)


def _fn(i: int) -> Node:
    return Node(id=f"n{i}", label="Function", name=f"f{i}", fqn=f"f{i}", repo="t", kind="method")


def _edge_key(e: Edge):
    return (e.type, e.src, e.dst, e.confidence, e.strategy, e.evidence_file, e.evidence_line)


def _rng():
    r = random.Random()
    r.seed(1234)  # deterministic; Math.random-free
    return r


def test_disk_edge_store_roundtrip():
    r = _rng()
    edges = [Edge("CALLS", f"n{r.randint(0,50)}", f"n{r.randint(0,50)}", evidence_line=i) for i in range(2500)]
    with tempfile.TemporaryDirectory() as d:
        store = DiskEdgeStore(d, shard_size=100)
        store.extend(edges)
        assert len(store) == len(edges)
        got = list(store)
        assert [_edge_key(e) for e in got] == [_edge_key(e) for e in edges]  # order preserved
        # re-iterable
        assert len(list(store)) == len(edges)
        # shard-by-shard covers everything exactly once
        via_shards = [e for shard in store.shards() for e in shard]
        assert [_edge_key(e) for e in via_shards] == [_edge_key(e) for e in edges]


def _poly_fixture():
    """Class hierarchy with overrides + callers, mirroring what derive sees."""
    r = _rng()
    nodes = [_fn(i) for i in range(40)]
    edges: list[Edge] = []
    # ancestors 0..4 each overridden by a few children
    overrides = []
    for anc in range(5):
        for k in range(r.randint(1, 4)):
            child = 10 + anc * 4 + k
            overrides.append(Edge("OVERRIDES", f"n{child}", f"n{anc}",
                                  confidence=Confidence.INFERRED.value, origin=Origin.EXTRACTED.value))
    # callers of ancestors (and some direct calls to children, to exercise dedup)
    calls = []
    for i in range(600):
        caller = 30 + r.randint(0, 9)
        tgt = r.randint(0, 20)
        calls.append(Edge("CALLS", f"n{caller}", f"n{tgt}", evidence_line=i, evidence_file="x.py"))
    return nodes, overrides, calls


def test_polymorphic_equivalence():
    nodes, overrides, calls = _poly_fixture()
    all_edges = overrides + calls

    # in-RAM original (reads OVERRIDES + CALLS from one list)
    orig = _synthesize_polymorphic_calls(all_edges)

    # streamed: OVERRIDES stay in RAM (structural), CALLS streamed from disk
    with tempfile.TemporaryDirectory() as dd:
        store = DiskEdgeStore(dd, shard_size=50)
        store.extend(calls)
        streamed = streaming_polymorphic_calls(overrides, store)

    assert {_edge_key(e) for e in orig} == {_edge_key(e) for e in streamed}
    assert len(orig) == len(streamed)  # no dup divergence


def test_polymorphic_no_overrides_is_empty():
    with tempfile.TemporaryDirectory() as dd:
        store = DiskEdgeStore(dd)
        store.extend([Edge("CALLS", "n1", "n2")])
        assert streaming_polymorphic_calls([], store) == []

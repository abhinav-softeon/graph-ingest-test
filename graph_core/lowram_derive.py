"""Option A — low-RAM derive building blocks.

The default pipeline holds the entire edge set (100M+ Edge objects) in one Python
list through SCIP -> dataflow -> derive -> write. For very large graphs on a small
box that is the RAM wall. This module provides the pieces to stream edges from disk
instead:

  - ``DiskEdgeStore``: an append-only, re-iterable edge container backed by pickle
    shards on disk. Drop-in for the derive passes' ``for e in edges`` (iteration +
    ``len``), but NOT indexable. Keeps ~one shard-buffer in RAM.
  - ``streaming_polymorphic_calls``: the one remaining derive step whose in-RAM
    version builds an edge-count-sized structure; rewritten to stream, and
    unit-tested to be byte-identical to the original in pipeline.py.
    (``streaming_call_metrics`` — fan_in/fan_out/recursive — was removed along
    with pipeline._attach_call_metrics; see MEMORY_ARCHITECTURE_PLAN.md item #8.)

The other derive passes (_derive_overrides, _derive_sql_links, validate_graph)
build only O(nodes)/O(component-pairs) state, so they run unchanged over a
streamed edge source — no reimplementation, no correctness risk.

Wiring this into index_repo (spill after dataflow, stream through derive, write
shard-by-shard) is gated behind config.get_lowram_derive(); the proven in-RAM path
is the default and untouched.
"""
from __future__ import annotations

import os
import pickle
from collections import defaultdict
from typing import Callable, Iterator

from .models import Confidence, Edge, Origin


class DiskEdgeStore:
    """Append-only, re-iterable edge container backed by pickle shards.

    Holds at most one ``shard_size``-buffer of Edge objects in RAM; ``__iter__``
    streams every shard back from disk. Supports ``append``/``extend``/``len`` and
    repeated iteration (each derive pass re-reads from disk). Not indexable.
    """

    def __init__(self, dirpath: str, shard_size: int = 1_000_000):
        self._dir = dirpath
        self._shard_size = max(1, shard_size)
        os.makedirs(dirpath, exist_ok=True)
        self._buf: list[Edge] = []
        self._shards: list[str] = []
        self._count = 0

    def append(self, e: Edge) -> None:
        self._buf.append(e)
        self._count += 1
        if len(self._buf) >= self._shard_size:
            self._flush()

    def extend(self, edges) -> None:
        for e in edges:
            self.append(e)

    def _flush(self) -> None:
        if not self._buf:
            return
        path = os.path.join(self._dir, f"edges_{len(self._shards):06d}.pkl")
        with open(path, "wb") as fh:
            pickle.dump(self._buf, fh, protocol=pickle.HIGHEST_PROTOCOL)
        self._shards.append(path)
        self._buf = []

    def seal(self) -> None:
        """Flush the in-RAM buffer to a final shard so iteration is complete."""
        self._flush()

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Edge]:
        self.seal()
        for path in self._shards:
            with open(path, "rb") as fh:
                batch = pickle.load(fh)
            yield from batch

    def shards(self) -> Iterator[list[Edge]]:
        """Yield each shard as a list — for shard-by-shard Neo4j writes so the
        write step never materializes all edges at once either."""
        self.seal()
        for path in self._shards:
            with open(path, "rb") as fh:
                yield pickle.load(fh)


class ChainedEdges:
    """Re-iterable, non-materializing view over several edge sources (lists
    and/or DiskEdgeStores). For the derive passes' ``for e in edges`` + ``len``.
    Not indexable — each iteration streams every source afresh (so a DiskEdgeStore
    source re-reads from disk)."""

    def __init__(self, *sources):
        self._sources = sources

    def __iter__(self) -> Iterator[Edge]:
        for s in self._sources:
            yield from s

    def __len__(self) -> int:
        return sum(len(s) for s in self._sources)


# Same cap as pipeline._POLY_FANOUT_GUARD — keep in sync.
_POLY_FANOUT_GUARD = 25


def streaming_polymorphic_calls(structural_edges: list[Edge], edges) -> list[Edge]:
    """Streaming equivalent of pipeline._synthesize_polymorphic_calls.

    The in-RAM original builds ``existing_calls = {(src,dst) for all CALLS}`` — an
    edge-count-sized set that defeats low-RAM. But that set is only ever probed as
    ``(caller.src, child)`` where ``child`` is a concrete override, so it is
    sufficient to remember existing CALLS whose dst is one of those children. That
    set is bounded by calls-into-overrides, not total calls.

    ``structural_edges`` carries the (small) OVERRIDES edges (kept in RAM);
    ``edges`` is the streamed CALLS source. Iterating ``edges`` in append order
    reproduces the original's ``[:GUARD]`` caller selection exactly, so the output
    is identical (verified in tests).
    """
    overrides_by_ancestor: dict[str, list[str]] = defaultdict(list)
    for e in structural_edges:
        if e.type == "OVERRIDES":
            overrides_by_ancestor[e.dst].append(e.src)
    if not overrides_by_ancestor:
        return []

    ancestors = set(overrides_by_ancestor)
    children_set = {c for children in overrides_by_ancestor.values() for c in children}

    # One streamed pass: callers of overridden ancestors, plus existing CALLS whose
    # dst is a concrete override (the only pairs the dedup check can ever match).
    callers_by_ancestor: dict[str, list[Edge]] = defaultdict(list)
    existing_into_children: set[tuple[str, str]] = set()
    for e in edges:
        if e.type != "CALLS":
            continue
        if e.dst in ancestors:
            callers_by_ancestor[e.dst].append(e)
        if e.dst in children_set:
            existing_into_children.add((e.src, e.dst))

    synthetic: list[Edge] = []
    for ancestor, children in overrides_by_ancestor.items():
        caller_edges = callers_by_ancestor.get(ancestor, [])[:_POLY_FANOUT_GUARD]
        if not caller_edges:
            continue
        for child in children:
            for ce in caller_edges:
                if (ce.src, child) in existing_into_children:
                    continue
                existing_into_children.add((ce.src, child))
                synthetic.append(Edge(
                    "CALLS", ce.src, child,
                    confidence=Confidence.AMBIGUOUS.value,
                    origin=Origin.DERIVED.value,
                    extractor="heuristic", strategy="polymorphic_dispatch",
                    evidence_file=ce.evidence_file, evidence_line=ce.evidence_line,
                ))
    return synthetic

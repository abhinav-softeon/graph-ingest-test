"""Experimental parallel resolve() — opt-in via GRAPH_RESOLVE_WORKERS>1.

NOT a copy from developer_assistant — new code for this app. resolver.py's
sequential ``resolve()`` (a single ``for ref in refs:`` loop) is left
completely untouched; GRAPH_RESOLVE_WORKERS=1 (the default) always uses it,
so this module is a strictly opt-in alternative for experimentation.

Design: split ``refs`` into N chunks and resolve each chunk in its own
worker process by calling the existing, unmodified ``resolve()`` once per
chunk (each worker independently rebuilds the same read-only lookup indices
from the full nodes/edges list — simpler and safer than trying to share a
single copy across processes, at the cost of some redundant CPU/memory per
worker). Then merge:
  - extra_nodes: concatenate, dedupe by ``.id`` — safe because ``make_id()``
    (ids.py) is a deterministic hash of repo+kind+fqn, so two workers
    resolving the same annotation/event name independently produce
    IDENTICAL node ids; no cross-worker coordination is needed.
  - edges: concatenate — every edge a chunk produces is unique to that
    chunk's refs, no dedup needed.
  - coverage: sum every Coverage field across workers, per ref type.

KNOWN LIMITATION (Windows): ProcessPoolExecutor uses `spawn` on Windows (no
`fork`), so nodes/edges are pickled to EVERY worker instead of shared
copy-on-write the way the original (POSIX) design assumed — this can raise
peak memory instead of lowering it. Use instrumentation/run_report.py's
output to measure whether parallel resolve actually helps on your machine;
don't assume it does.

NOT supported in v1 (pipeline.py falls back to sequential resolve() with a
logged warning instead): the streaming-writer edge_sink path, and refs
supplied as a RefStream (streaming-ingest) rather than a plain list.
"""
from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable, Optional

from .models import Edge, IngestCancelled, Node, RawRef
from .resolver import Coverage, resolve
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


def _resolve_chunk(nodes: list[Node], edges: list[Edge], refs_chunk: list[RawRef], repo: str):
    """Top-level (picklable, for ProcessPoolExecutor) per-chunk worker —
    just calls the untouched sequential resolve() with this chunk's refs."""
    return resolve(nodes, edges, refs_chunk, repo)


def _chunk(seq: list, n: int) -> list[list]:
    if n <= 1 or len(seq) <= 1:
        return [seq]
    size = max(1, -(-len(seq) // n))  # ceil division
    return [seq[i:i + size] for i in range(0, len(seq), size)] or [[]]


def parallel_resolve(
    nodes: list[Node],
    edges: list[Edge],
    refs: list[RawRef],
    repo: str,
    workers: int,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
):
    """Return (extra_nodes, edges, coverage) — same shape as resolver.resolve().

    ``refs`` must already be a plain list (not a RefStream); the caller
    (pipeline.py) only routes here when that holds — see module docstring.
    """
    chunks = [c for c in _chunk(refs, workers) if c]
    total = len(refs)
    _log.info(
        "[resolve_parallel][repo=%s] splitting %s ref(s) into %s chunk(s) across %s worker(s)",
        repo, total, len(chunks), workers,
    )
    t0 = time.monotonic()
    all_extra_nodes: list[Node] = []
    all_edges: list[Edge] = []
    coverage_by_type: dict[str, Coverage] = {}
    done_refs = 0
    if on_progress:
        on_progress(0, total)

    with ProcessPoolExecutor(max_workers=workers) as pool:
        future_to_size = {
            pool.submit(_resolve_chunk, nodes, edges, chunk, repo): len(chunk)
            for chunk in chunks
        }
        for fut in as_completed(future_to_size):
            if cancel_check and cancel_check():
                for f in future_to_size:
                    f.cancel()
                raise IngestCancelled("resolve cancelled")
            chunk_extra_nodes, chunk_edges, chunk_coverage = fut.result()
            all_extra_nodes.extend(chunk_extra_nodes)
            all_edges.extend(chunk_edges)
            for ref_type, cov in chunk_coverage.items():
                agg = coverage_by_type.setdefault(ref_type, Coverage())
                agg.total += cov.total
                agg.resolved += cov.resolved
                agg.ambiguous += cov.ambiguous
                agg.unresolved += cov.unresolved
                agg.external += cov.external
            done_refs += future_to_size[fut]
            _log.info(
                "[resolve_parallel][repo=%s] chunk done: %s/%s ref(s) resolved so far (%.1fs elapsed)",
                repo, done_refs, total, time.monotonic() - t0,
            )
            if on_progress:
                on_progress(done_refs, total)

    seen_ids: set[str] = set()
    deduped_nodes: list[Node] = []
    for n in all_extra_nodes:
        if n.id not in seen_ids:
            seen_ids.add(n.id)
            deduped_nodes.append(n)

    _log.info(
        "[resolve_parallel][repo=%s] merged %s worker chunk(s): %s extra node(s) (deduped from %s), "
        "%s edge(s) in %.1fs",
        repo, len(chunks), len(deduped_nodes), len(all_extra_nodes), len(all_edges), time.monotonic() - t0,
    )
    return deduped_nodes, all_edges, coverage_by_type

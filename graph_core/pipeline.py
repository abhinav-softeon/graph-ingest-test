"""Phase 0 orchestration: discover -> extract -> resolve -> write."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from queue import Queue
from typing import Callable

import psutil

from . import extractors
from .canonical_ir import CanonicalBundle, from_extractor, merge_bundles
from .cypher_derive_reference import synthesize_polymorphic_calls_cypher
from .checkpoint import RefStream, batch_exists, clear_checkpoint, init_manifest, load_batch, save_batch
from .config import checkpoint_root as get_checkpoint_root
from .config import (
    extract_batch_size,
    extract_worker_count,
    get_cache_io_workers,
    is_streaming_ingest_enabled,
    javac_batch_size,
    javac_timeout_seconds,
)
from .discovery import FileInfo, discover, list_candidate_relpaths
from .extract_cache import get_extract_cache
from .ids import make_id
from .models import Confidence, Edge, Node, Origin, RawRef, _clean
from .resolver import Coverage, resolve
from .javac_resolver import (
    JavacReport,
    resolve_java_calls,
)
from .store import GraphStore
from .validator import EdgeStats, validate_graph
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# The ONLY edge types retained in RAM past the resolve sink. The derive passes
# re-read these arbitrarily: CONTAINS builds the parent/scope chain,
# EXTENDS/IMPLEMENTS build the class hierarchy for _derive_overrides,
# ANNOTATED_WITH/EXPOSES are structural and tiny. All are bounded by
# DECLARATIONS rather than call sites — roughly 2-3 per node — so they are
# orders of magnitude fewer than the CALLS bulk.
#
# DEFINES was here, and was an exact duplicate of CONTAINS: every extractor
# emitted the two together from the same (container, child, evidence) triple,
# so the only CONTAINS without a DEFINES twin were the derived package-tree
# edges. Nothing ever read it — not resolve (which matches CONTAINS for the
# scope chain), not the derive passes, not validate_graph, and not a single
# Cypher query in developer_assistant's analyzer, all of which use explicitly
# typed relationships. It was write-only: ~half the retained structural edge
# set and a duplicate MERGE per declaration, for no consumer. Recoverable
# exactly as `CONTAINS where src.label NOT IN ('Repository','Package')` if the
# distinction is ever actually wanted.
#
# Everything else (the 100M+ CALLS and friends) is written to Neo4j by the sink
# and then dropped on the floor: nothing downstream reads it. That is item #2b,
# and it only became possible once validate_graph stopped scanning edges
# (item #13's EdgeStats) and polymorphic dispatch moved into the database
# (item #12) — those were the last two full-edge-set consumers.
_RETAINED_EDGE_TYPES = frozenset({
    "CONTAINS", "EXTENDS", "IMPLEMENTS", "ANNOTATED_WITH", "EXPOSES",
})

OnStage = "Callable[[str, dict], None] | None"


def _peak_rss_mb() -> float:
    """Peak-so-far RSS of THIS process, cross-platform via psutil (standalone
    app has no POSIX-only `resource` module dependency). This is the main
    process only — see instrumentation/memory_sampler.py for a sampler that
    also sums child worker-process RSS during parallel stages, which is what
    this app's run reports actually use for overall/peak figures."""
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _extract_one(file: FileInfo, repo: str) -> tuple[list, list, list]:
    """Top-level (picklable, for ProcessPoolExecutor) wrapper around a single
    file's extraction. Isolates one bad/huge file's failure to that file
    instead of crashing the whole parallel batch — the old sequential loop
    had no such guard, but a silent per-file skip is the safer default once
    files run across separate worker processes."""
    try:
        return extractors.extract(file, repo)
    except Exception:
        return [], [], []


@dataclass
class IndexResult:
    repo: str
    files: int = 0
    nodes: int = 0
    edges: int = 0
    seconds: float = 0.0
    coverage: dict[str, Coverage] = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    db_counts: dict = field(default_factory=dict)
    javac: JavacReport = field(default_factory=JavacReport)
    # `roles: dict` lived here — role-classification diagnostics. The pass that
    # produced them was deleted in item #3, so every construction below passed
    # a hardcoded {}. Removed.
    # `dfg: DataflowResult` lived here — the DFG pass and its stats were
    # removed with dfg_json (MEMORY_ARCHITECTURE_PLAN.md item #10).
    stage_seconds: dict[str, float] = field(default_factory=dict)  # per-stage timing breakdown


def index_repo(root: str, repo: str, store: GraphStore, wipe: bool = True,
               javac: bool = False, on_stage: OnStage = None,
               candidate_files: list[str] | None = None,
               cancel_check: Callable[[], bool] | None = None,
               changed_files: list[str] | None = None,
               deleted_files: list[str] | None = None) -> IndexResult:
    """``javac``: resolve Java CALLS with javac instead of heuristic name
    matching (graph_core/javac_resolver.py). Runs BEFORE the heuristic resolve
    so it replaces that work rather than adding to it — when it succeeds, Java
    CALLS refs are skipped in resolve() entirely instead of being resolved and
    then thrown away. Falls back silently to the heuristic on any failure.

    ``changed_files``/``deleted_files``: incremental-ingest mode. When either
    is given (even an empty list), ``wipe`` is ignored and only nodes belonging
    to those files are deleted from the namespace before the write below —
    every other file's previously-written nodes are left untouched. Extraction
    still runs over every file in ``candidate_files`` (unchanged files load
    their bundle from the extraction cache instead of being re-parsed), so the
    resolve/derive stages still see the whole codebase and cross-file edges
    stay globally correct."""
    incremental = changed_files is not None or deleted_files is not None
    extract_cache = get_extract_cache()
    cache_io_workers = get_cache_io_workers()
    # Fire-and-forget cache writes (local-disk in this standalone app):
    # submitting here (not calling put() directly) keeps the extraction
    # result loop from blocking on each write. A put() failure is already a
    # no-op (see extract_cache.py's docstring), so an unwaited future is
    # never a correctness issue — worst case this file's result just isn't
    # cached for next time. Drained (not abandoned) at the end of the
    # extraction phase.
    cache_put_pool = ThreadPoolExecutor(max_workers=cache_io_workers)
    t0 = time.time()
    stage_seconds: dict[str, float] = {}
    t_prev = t0

    def _beat(stage: str, step: str, **detail) -> None:
        """Heartbeat + log for a long-running step INSIDE a stage.

        The job's staleness watchdog (job_maintenance.recover_stale_running_jobs)
        keys off `heartbeat_at`, which only advances when on_stage fires. Several
        steps here run for many minutes without emitting anything — the
        pre-resolve node write, resolver index build, and each derive
        pass — so on a large repo a perfectly healthy job looked dead and got
        requeued (then, having used its one requeue, abandoned with "skipping
        redispatch"). Every such step now beats, and logs, so both the watchdog
        and a human tailing logs can see it is alive."""
        _log.info("[graph_ingest][repo=%s] %s: %s (peak_rss=%.0fMB)", repo, stage, step, _peak_rss_mb())
        if on_stage:
            on_stage(stage, {"step": step, **detail})

    def _write_beat(stage: str, what: str):
        """on_batch callback factory for store.write_nodes/write_edges."""
        def _cb(written: int, total: int) -> None:
            _beat(stage, f"{what} {written}/{total}")
        return _cb

    def _mark(label: str) -> None:
        nonlocal t_prev
        now = time.time()
        stage_seconds[label] = round(now - t_prev, 3)
        t_prev = now
        _log.info(
            "[graph_ingest][repo=%s] stage '%s' done in %.3fs (peak_rss=%.0fMB)",
            repo, label, stage_seconds[label], _peak_rss_mb(),
        )

    # Enumerate candidate file paths cheaply (no file reads) so we can chunk
    # discover()+extraction into batches below — this is the actual
    # peak-memory driver for very large repos: reading every file's content
    # into memory simultaneously (the old single discover() call) instead of
    # one batch at a time. Extraction results (nodes/edges/refs) are far
    # smaller than raw source text and are kept for the whole run exactly as
    # before — only the raw file-content memory lifecycle changes here.
    all_relpaths = candidate_files if candidate_files is not None else list_candidate_relpaths(root)
    total_files = len(all_relpaths)
    _log.info("[graph_ingest][repo=%s] discover: %s candidate files (batch_size=%s)", repo, total_files, extract_batch_size())
    if on_stage:
        on_stage("discovering", {"total_files": total_files})
    _mark("discovering")

    has_java = any(p.lower().endswith(".java") for p in all_relpaths)

    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    all_refs: list[RawRef] = []

    worker_count = min(extract_worker_count(), max(1, total_files))
    batch_size = extract_batch_size()
    batch_starts = list(range(0, total_files, batch_size)) if total_files else [0]
    num_batches = len(batch_starts)

    # Checkpointing (opt-in via GRAPH_CHECKPOINT_ROOT): each batch's merged
    # bundle is persisted to disk and — this is the actual point of it, and
    # previously didn't happen despite the old comment here claiming it did —
    # dropped from memory instead of also being kept in all_nodes/all_edges/
    # all_refs for the rest of the extraction phase. Those accumulate
    # unboundedly with codebase size otherwise (independent of batch size:
    # chunking only bounded the discover()/raw-file-read step, never this),
    # so a large repo's peak memory during extraction used to grow toward
    # "the whole codebase" well before resolve() even starts — exactly what
    # crashed a worker process on a ~20k-file repo. With checkpointing on,
    # the whole graph is reassembled from these batch files in one pass right
    # before resolve() (below), which is the first stage that actually needs
    # every node/edge/ref simultaneously. A retried run of the same repo tag
    # also resumes by skipping batches already marked done in the manifest,
    # rather than restarting extraction from file 1 — same mechanism, two
    # benefits.
    ckpt_root = get_checkpoint_root()
    manifest = init_manifest(ckpt_root, repo, num_batches, batch_size) if ckpt_root else None
    spilling = manifest is not None
    # Phase 3 (streaming ingest): when the extraction batches are on disk, stream
    # refs through resolve one batch at a time (checkpoint.RefStream) instead of
    # reassembling the whole multi-million-ref list into memory. Requires spilling
    # (no batch files on disk otherwise). streamed_ref_count is accumulated during
    # reassembly (which already loads every batch) so RefStream has an exact len
    # without an extra counting pass.
    stream_refs = spilling and is_streaming_ingest_enabled()
    # Node early-write + bulky-field-drop: unconditional (MEMORY_ARCHITECTURE_
    # PLAN.md item #1). Persisting extracted nodes to Neo4j immediately, then
    # dropping fields nothing downstream ever reads again, has no structural
    # dependency on disk-spill checkpointing — it only needs a reachable
    # Neo4j, which this app always requires anyway. Previously gated behind
    # stream_refs (disk-spill checkpointing) for no structural reason.
    stream_nodes = True
    # Edge early-write + SlimEdge conversion: unconditional (MEMORY_ARCHITECTURE_
    # PLAN.md item #2). Edges are flushed to Neo4j as they exist in final form
    # and only SlimEdge stand-ins are retained in RAM — full Edge objects
    # never accumulate for the whole repo. Depends on stream_nodes (edge
    # endpoints must already exist in Neo4j when edges are written) —
    # satisfied unconditionally now that stream_nodes always runs first, same
    # ordering guarantee as before, just no longer optional on either side.
    #
    # NOTHING is deferred any more. CALLS used to be held full in RAM whenever
    # SCIP might run, because scip_python/scip_java replaced whole languages'
    # CALLS *after* resolve — so the edges could not be written eagerly without
    # leaving stale rows behind. That deferral was a latent OOM: on a repo whose
    # CALLS are the overwhelming bulk, holding them all as full Edge objects
    # through resolve defeats the entire streaming-writer design.
    #
    # The javac resolver avoids the problem by construction: it runs BEFORE
    # resolve, and when it succeeds the heuristic simply never produces Java
    # CALLS to be replaced. There is nothing to rewrite, so everything streams.
    # PASSES was the other deferred type; both it and the DFG pass are gone
    # (items #9/#10).
    stream_writer = True
    defer_edge_types: set[str] = set()
    streamed_ref_count = 0

    # A GRAPH_LOWRAM_DERIVE admission check lived here. The flag spilled the
    # bulk edges to disk so derive could stream them; with nothing left that
    # reads the bulk (items #12/#2b) the spill became write-only, so the whole
    # path was removed — the default path now gives what it was invented for,
    # without the disk I/O. See MEMORY_ARCHITECTURE_PLAN.md item #16.

    if manifest is not None:
        _log.info(
            "[graph_ingest][repo=%s] checkpointing enabled: %s batch(es), %s already done",
            repo, num_batches, len(manifest["batches_done"]),
        )

    done = 0
    last_report = t0
    last_file_lang = ("", "")
    for batch_idx, batch_start in enumerate(batch_starts):
        batch_relpaths = all_relpaths[batch_start:batch_start + batch_size] if total_files else []

        if manifest is not None and batch_idx in manifest["batches_done"]:
            # Cheap existence check only — do NOT fully deserialize here just
            # to immediately discard the result. That data is actually loaded
            # once, for real, in the read-back pass right after this loop;
            # calling load_batch() here too (as an earlier version of this
            # check did) meant every resumed batch got deserialized from disk
            # twice on a checkpoint resume, for no benefit.
            if batch_exists(ckpt_root, repo, batch_idx):
                done += len(batch_relpaths)
                if on_stage:
                    on_stage("graph_parsing", {
                        "done": done, "total": total_files,
                        "current_file": "(resumed from checkpoint)", "language": "",
                    })
                continue
            # Checkpoint file missing/corrupt despite being marked done —
            # fall through and reprocess this batch normally.

        # discover() here is a real, sometimes-slow step (reads + hashes up
        # to batch_size files from disk) — report it before it starts, not
        # just after the first file finishes, so a slow batch doesn't look
        # like a silent stall between the "discovering" stage and the first
        # "graph_parsing" progress tick.
        _log.info(
            "[graph_ingest][repo=%s] batch %s/%s: reading+hashing %s file(s)",
            repo, batch_idx + 1, num_batches, len(batch_relpaths),
        )
        if on_stage:
            on_stage("graph_parsing", {
                "done": done, "total": total_files,
                "current_file": f"(reading batch {batch_idx + 1}/{num_batches})", "language": "",
            })
        batch_files = discover(root, candidate_relpaths=batch_relpaths) if total_files else []
        batch_bundles: list[CanonicalBundle] = []

        # Content-hash-keyed extraction cache: a file whose sha was already
        # extracted (in any previous run, for any repo) doesn't need
        # tree-sitter parsing again — load its bundle straight from the
        # cache and only submit true cache-misses to the pool below.
        #
        # Each lookup is an S3 round-trip — threaded (I/O-bound), not
        # sequential, since a serial loop here means up to batch_size network
        # calls (with zero progress reporting) before parsing can even start.
        # On a cold cache (e.g. a codebase's first-ever ingest) every single
        # lookup misses, so this used to be pure serial network latency for
        # no benefit at all.
        cache_misses: list[FileInfo] = []
        if batch_files:
            get_done = 0
            with ThreadPoolExecutor(max_workers=min(cache_io_workers, len(batch_files))) as io_pool:
                future_to_file = {io_pool.submit(extract_cache.get, f.sha, repo, f.relpath): f for f in batch_files}
                for fut in as_completed(future_to_file):
                    f = future_to_file[fut]
                    cached_bundle = fut.result()
                    get_done += 1
                    if cached_bundle is None:
                        cache_misses.append(f)
                        continue
                    batch_bundles.append(cached_bundle)
                    done += 1
                    last_file_lang = (f.relpath, f.lang)
                    if on_stage and (done % 200 == 0 or get_done == len(batch_files)):
                        on_stage("graph_parsing", {
                            "done": done, "total": total_files,
                            "current_file": f.relpath, "language": f.lang,
                        })

        if worker_count > 1 and len(cache_misses) > 1:
            # A worker process can be killed outright (the OS/cgroup OOM-
            # killer targeting just that one process, not necessarily the
            # whole container — e.g. one particular file spikes memory in
            # its worker) — unlike a plain exception inside _extract_one
            # (already caught there and turned into an empty result), a
            # killed worker poisons the *entire* pool: every other
            # pending/in-flight future in it raises BrokenProcessPool too,
            # which used to take the whole batch (and everything already
            # extracted in it) down with it. Falling back to sequential,
            # one-file-at-a-time extraction for whatever didn't finish
            # isolates the blast radius to (at most) the one file that
            # actually caused it, instead of losing the batch.
            processed_relpaths: set[str] = set()
            pool_broken_exc: Exception | None = None
            try:
                with ProcessPoolExecutor(max_workers=worker_count) as pool:
                    future_to_file = {pool.submit(_extract_one, f, repo): f for f in cache_misses}
                    for fut in as_completed(future_to_file):
                        file_info = future_to_file[fut]
                        nodes, edges, refs = fut.result()
                        bundle = from_extractor(nodes, edges, refs)
                        batch_bundles.append(bundle)
                        cache_put_pool.submit(extract_cache.put, file_info.sha, repo, file_info.relpath, bundle)
                        processed_relpaths.add(file_info.relpath)
                        done += 1
                        last_file_lang = (file_info.relpath, file_info.lang)
                        now = time.time()
                        if on_stage and (done % 200 == 0 or now - last_report >= 3.0):
                            on_stage("graph_parsing", {
                                "done": done,
                                "total": total_files,
                                "current_file": file_info.relpath,
                                "language": file_info.lang,
                            })
                            last_report = now
            except BrokenProcessPool as exc:
                pool_broken_exc = exc

            if pool_broken_exc is not None:
                stragglers = [f for f in cache_misses if f.relpath not in processed_relpaths]
                _log.warning(
                    "[graph_ingest][repo=%s] extraction worker crashed mid-batch (%s) — "
                    "falling back to sequential extraction for the %s remaining file(s)",
                    repo, pool_broken_exc, len(stragglers),
                )
                for f in stragglers:
                    _log.info("[graph_ingest][repo=%s] sequential fallback: extracting %s", repo, f.relpath)
                    nodes, edges, refs = _extract_one(f, repo)
                    bundle = from_extractor(nodes, edges, refs)
                    batch_bundles.append(bundle)
                    cache_put_pool.submit(extract_cache.put, f.sha, repo, f.relpath, bundle)
                    done += 1
                    last_file_lang = (f.relpath, f.lang)
                    if on_stage and done % 200 == 0:
                        on_stage("graph_parsing", {
                            "done": done, "total": total_files,
                            "current_file": f.relpath, "language": f.lang,
                        })
        else:
            for f in cache_misses:
                nodes, edges, refs = _extract_one(f, repo)
                bundle = from_extractor(nodes, edges, refs)
                batch_bundles.append(bundle)
                cache_put_pool.submit(extract_cache.put, f.sha, repo, f.relpath, bundle)
                done += 1
                last_file_lang = (f.relpath, f.lang)
                if on_stage and done % 200 == 0:
                    on_stage("graph_parsing", {
                        "done": done,
                        "total": total_files,
                        "current_file": f.relpath,
                        "language": f.lang,
                    })

        # Merge within this batch only (dedup within the batch) — a second,
        # final merge across all batches happens after the loop to catch any
        # cross-batch duplicate node/edge/ref ids (e.g. a package node
        # independently emitted by two files in different batches), exactly
        # matching the original single-global-merge behavior.
        batch_merged = merge_bundles(batch_bundles) if batch_bundles else CanonicalBundle(nodes=[], edges=[], refs=[])

        if spilling:
            # Persist to disk and do NOT also extend all_nodes/all_edges/
            # all_refs — that would defeat the point (see the checkpointing
            # comment above index_repo's batch loop). Read back once, for
            # every batch, right after this loop instead.
            save_batch(ckpt_root, repo, batch_idx, batch_merged, manifest)
        else:
            # No checkpoint root configured — same behavior as before this
            # change: accumulate in memory as we go, nothing spilled to disk.
            all_nodes.extend(batch_merged.nodes)
            all_edges.extend(batch_merged.edges)
            all_refs.extend(batch_merged.refs)
        # batch_files/batch_bundles/batch_merged go out of scope here —
        # eligible for GC before the next batch is discovered/read.

    if on_stage:
        on_stage("graph_parsing", {
            "done": total_files,
            "total": total_files,
            "current_file": last_file_lang[0],
            "language": last_file_lang[1],
        })

    # Drain outstanding cache writes before moving on — bounds how long a
    # crash/restart could lose in-flight uploads, without having blocked the
    # extraction loop above on each one individually.
    cache_put_pool.shutdown(wait=True)

    if spilling:
        # Reassemble the full graph from disk in one pass — this is the
        # first point since extraction started where every node/edge/ref
        # needs to be resident in memory simultaneously, because resolve()
        # right below needs the whole thing for cross-file reference
        # resolution. Every batch was written to disk as it finished instead
        # of being kept here too, so peak memory during the (often much
        # longer) extraction loop above stayed bounded to ~one batch, not
        # "the whole codebase so far".
        _log.info("[graph_ingest][repo=%s] reassembling %s batch(es) from checkpoint for resolve", repo, num_batches)
        if on_stage:
            on_stage("graph_parsing", {
                "done": total_files, "total": total_files,
                "current_file": f"(reassembling batch 1/{num_batches} from checkpoint)", "language": "",
            })
        _t_reassemble = time.time()
        _last_reassemble_report = _t_reassemble
        for batch_idx in range(num_batches):
            batch = load_batch(ckpt_root, repo, batch_idx)
            if batch is not None:
                all_nodes.extend(batch.nodes)
                all_edges.extend(batch.edges)
                if stream_refs:
                    # Don't materialize refs — count them here (batch already
                    # loaded) and let RefStream re-read them from disk in resolve.
                    streamed_ref_count += len(batch.refs)
                else:
                    all_refs.extend(batch.refs)
            now = time.time()
            if now - _last_reassemble_report >= 3.0 or batch_idx == num_batches - 1:
                _last_reassemble_report = now
                _log.info(
                    "[graph_ingest][repo=%s] reassembling: %s/%s batch(es) loaded (%s nodes, %s edges, %s refs so far)",
                    repo, batch_idx + 1, num_batches, len(all_nodes), len(all_edges),
                    streamed_ref_count if stream_refs else len(all_refs),
                )
                if on_stage:
                    on_stage("graph_parsing", {
                        "done": total_files, "total": total_files,
                        "current_file": f"(reassembling batch {batch_idx + 1}/{num_batches} from checkpoint)",
                        "language": "",
                    })
        _log.info("[graph_ingest][repo=%s] reassembling: done in %.3fs", repo, time.time() - _t_reassemble)

    # Final cross-batch merge: within-batch merges already deduped nodes/edges
    # /refs *within* each batch above; this pass catches duplicates that span
    # batches (e.g. a package node independently emitted by files in two
    # different batches) — mathematically identical to the original single
    # whole-run merge_bundles() call, just split into two passes so each
    # batch's raw per-file bundles never all coexist in memory at once.
    _log.info(
        "[graph_ingest][repo=%s] final merge: deduping %s node(s), %s edge(s), %s ref(s) across batches",
        repo, len(all_nodes), len(all_edges), streamed_ref_count if stream_refs else len(all_refs),
    )
    if on_stage:
        on_stage("graph_parsing", {
            "done": total_files, "total": total_files,
            "current_file": "(final merge — deduping across batches)", "language": "",
        })
    _t_merge = time.time()
    final_merged = merge_bundles([CanonicalBundle(nodes=all_nodes, edges=all_edges, refs=all_refs)])
    all_nodes = final_merged.nodes
    all_edges = final_merged.edges
    all_refs = final_merged.refs
    _log.info("[graph_ingest][repo=%s] final merge: done in %.3fs", repo, time.time() - _t_merge)
    _mark("graph_parsing")
    _log.info(
        "[graph_ingest][repo=%s] extracting: %s nodes, %s edges, %s refs so far",
        repo, len(all_nodes), len(all_edges), streamed_ref_count if stream_refs else len(all_refs),
    )

    # Edge tallies for validate_graph, accumulated as edges are produced instead
    # of scanned once at the end (see validator.EdgeStats). `known_node_ids` is
    # kept in step with node creation so the dangling check can be answered at
    # add time; every edge's endpoints exist by the time it is counted (the
    # extraction bundle is self-consistent, and the resolve sink hands over a
    # batch's new nodes before the batch).
    edge_stats = EdgeStats()
    known_node_ids: set[str] = {n.id for n in all_nodes}
    edge_stats.add(all_edges, known_node_ids)

    # Refs source for resolve: RefStream (disk-backed, one batch at a time) under
    # streaming ingest, else the in-memory all_refs list (unchanged path).
    refs_source = RefStream(ckpt_root, repo, num_batches, total=streamed_ref_count) if stream_refs else all_refs

    if on_stage:
        # call_ref_count needs a full ref scan — skip it under streaming (it would
        # cost an extra disk pass just for a progress label); total_refs is cheap
        # (RefStream carries a precomputed len).
        call_ref_count = 0 if stream_refs else sum(1 for r in all_refs if r.type == "CALLS")
        on_stage("resolving", {
            "mode": "heuristic",
            "total_refs": len(refs_source),
            "call_refs": call_ref_count,
        })

    def _resolve_progress(done: int, total: int) -> None:
        _log.info("[graph_ingest][repo=%s] resolving %s/%s refs (peak_rss=%.0fMB)", repo, done, total, _peak_rss_mb())
        if on_stage:
            on_stage("resolving", {"mode": "heuristic", "done": done, "total": total})

    # Phase 2 (streaming): persist the extracted nodes to Neo4j NOW — before the
    # memory-heavy resolve/derive tail — then drop their bulky/unused fields from
    # RAM (none are read after extraction by resolve/scip/derive — see
    # MEMORY_ARCHITECTURE_PLAN.md item #1 for the full field-by-field audit;
    # notably end_line/param_names/body_hash/repo are NOT cleared here — they
    # ARE read downstream, by scip_resolver.py/_derive_sql_links).
    # Neo4j is their source of truth from here. Wipe/delete must precede this
    # early write. Nodes created later (resolve's synthesized nodes, package
    # nodes) are tracked in late_nodes and written at the end. No derive pass
    # sets a node property any more (items #3/#8/#10), so nothing needs patching
    # back onto the already-written nodes — the final Neo4j state matches one
    # full write.
    # Precise Java CALLS, BEFORE resolve. Ordering is the whole design: SCIP ran
    # after resolve and replaced its output, which meant paying for the heuristic
    # pass AND holding every CALLS edge in RAM waiting to be superseded. Running
    # first means resolve simply never does the work (see skip_call_langs below),
    # and nothing needs deferring.
    #
    # all_nodes is complete here — javac matches its bindings onto node ids by
    # (fqn, param_count), and both survive the slim projection further down.
    javac_report = JavacReport()
    javac_owns_java = False
    javac_edges: list[Edge] = []
    if javac and has_java:
        if on_stage:
            on_stage("javac", {})
        _beat("javac", "resolving Java calls with javac")
        javac_edges, javac_report = resolve_java_calls(
            all_nodes, root, repo,
            timeout=javac_timeout_seconds(), batch_size=javac_batch_size(),
        )
        javac_owns_java = javac_report.available
        if not javac_owns_java:
            _log.info(
                "[graph_ingest][repo=%s] javac unavailable (%s) — Java stays on "
                "the heuristic resolver", repo, javac_report.reason,
            )
    _mark("javac")

    late_nodes: list[Node] = []
    if stream_nodes:
        store.bootstrap()
        if incremental:
            store.delete_files(repo, sorted(set(changed_files or []) | set(deleted_files or [])))
        elif wipe:
            store.wipe(repo)
        _beat("resolving", f"writing {len(all_nodes)} extracted node(s) to Neo4j")
        store.write_nodes(all_nodes, on_batch=_write_beat("resolving", "nodes written"))
        _log.info("[graph_ingest][repo=%s] streaming ingest: wrote %s extracted node(s) to Neo4j pre-resolve", repo, len(all_nodes))
        # Replace every extracted node with its slim projection, IN PLACE so the
        # full object is freed as we go rather than briefly holding both lists.
        # This used to be a field-blanking loop that zeroed the bulky values but
        # kept the 40-slot Node shell alive to the end of the run; projecting
        # instead drops the shell too (360 B -> 160 B per node) and, because the
        # slim record now carries the FULL post-resolve read set, needs no second
        # projection for resolve. Neo4j is these nodes' source of truth from here.
        #
        # Nodes created later (resolve's synthesized nodes, package nodes) stay
        # full Node objects — they have not been written yet and store.write_nodes
        # needs .props(). So all_nodes is a mixed list from here on, exactly as
        # all_edges is for SlimEdge; every consumer past this point reads only
        # SLIM_NODE_FIELDS, which test_slim_node.py enforces by AST scan.
        _beat("resolving", f"projecting {len(all_nodes)} node(s) to the slim record")
        for _i in range(len(all_nodes)):
            all_nodes[_i] = all_nodes[_i].to_slim()

        if stream_writer:
            # Step 11: the structural (extraction-emitted) edges are in final
            # form already — flush them to Neo4j now and keep only SlimEdge
            # stand-ins, except the deferred types (see defer_edge_types).
            # Endpoints all exist (extracted nodes were just written above).
            # resolve() below reads only type/src/dst off these (CONTAINS scope
            # chain), which SlimEdge carries.
            #
            # Writes go through a single background writer thread so the
            # CPU-bound resolve loop below overlaps the Neo4j write latency
            # instead of alternating with it. One thread + FIFO queue preserves
            # write order exactly (each item's nodes before its edges; items in
            # emission order); the bounded queue caps how many full-object
            # batches await write (backpressure). Joined — and any failure
            # re-raised — immediately after resolve returns, before any stage
            # that assumes the writes are durable. The queued lists hold the
            # ORIGINAL full Edge objects; the in-RAM slimming below builds new
            # stand-ins without mutating them, so there is no write race.
            write_queue: Queue = Queue(maxsize=3)
            writer_failure: list[BaseException] = []

            def _writer_loop() -> None:
                while True:
                    item = write_queue.get()
                    if item is None:
                        write_queue.task_done()
                        return
                    nodes_batch, edges_batch = item
                    try:
                        if nodes_batch:
                            store.write_nodes(nodes_batch)
                        if edges_batch:
                            store.write_edges(edges_batch)
                    except BaseException as exc:  # noqa: BLE001 - re-raised by the producer
                        writer_failure.append(exc)
                        # Keep consuming so the producer never blocks on a dead
                        # consumer; every later enqueue raises immediately.
                    finally:
                        write_queue.task_done()

            writer_thread = threading.Thread(target=_writer_loop, name="graph-edge-writer", daemon=True)
            writer_thread.start()

            def _enqueue_write(nodes_batch: list[Node], edges_batch: list[Edge]) -> None:
                if writer_failure:
                    raise writer_failure[0]
                write_queue.put((nodes_batch, edges_batch))

            _flush = [e for e in all_edges if e.type not in defer_edge_types]
            if _flush:
                _beat("resolving", f"queueing {len(_flush)} structural edge(s) for write")
                _enqueue_write([], _flush)
            all_edges = [e if e.type in defer_edge_types else e.to_slim() for e in all_edges]
            _log.info(
                "[graph_ingest][repo=%s] streaming writer: flushed %s structural edge(s) pre-resolve (%s deferred)",
                repo, len(_flush), len(all_edges) - len(_flush),
            )

    # Phase 1 (streaming ingest): resolve against a SlimNode projection instead
    # of full Node objects. The resolver reads only the slim fields (enforced by
    # test_slim_node.py's guardrail), so this produces byte-identical edges — it
    # is the correctness scaffold the regression oracle validates before Phase 2
    # stops holding the full nodes at all. Off by default (full nodes passed).
    # all_nodes is ALREADY the slim projection (see the pre-resolve write above),
    # so resolve reads it directly. This used to build a SECOND slim list while
    # the full one stayed alive — 360 B + 128 B per node held simultaneously
    # through the longest phase of the run.
    resolve_nodes = all_nodes
    _log.info(
        "[graph_ingest][repo=%s] resolving against slim projection of %s node(s)%s",
        repo, len(resolve_nodes), " (refs streamed from disk)" if stream_refs else "",
    )
    # Disable the resolve-state checkpoint under streaming: it re-serializes the
    # entire (growing) out_edges list to disk every 60s/200k refs, which is
    # O(n^2) I/O on a multi-million-ref repo — the actual cause of the mid-resolve
    # stall (CPU idle, disk pegged writing an ever-larger blob). The expensive
    # phase (extraction) is still crash-resumable via its own batch checkpoint;
    # re-running resolve from scratch on a crash is cheap by comparison once it's
    # not I/O-choked. (Proper fix — flush edges to Neo4j + checkpoint only
    # progress metadata — is the streaming-writer rework; see TIER3_MEMORY_PLAN.)
    resolve_ckpt = None if stream_refs else ckpt_root

    # Step 11: sink handed to resolve() — hands each ~10k-edge batch (preceded
    # by that batch's just-synthesized nodes, so MATCH-based edge writes find
    # their endpoints) to the background writer, and returns SlimEdge stand-ins
    # to retain, except the deferred types which stay full until the
    # post-dataflow write.
    resolve_sink = None
    if stream_writer:
        def resolve_sink(new_nodes: list[Node], batch: list[Edge]) -> list:
            known_node_ids.update(n.id for n in new_nodes)
            edge_stats.add(batch, known_node_ids)
            to_write = [e for e in batch if e.type not in defer_edge_types]
            if new_nodes or to_write:
                _enqueue_write(new_nodes, to_write)
            # Retain almost nothing (item #2b). A deferred edge is held in FULL
            # because it hasn't been written yet and a later stage rewrites it
            # (CALLS under SCIP). A structural edge is retained SLIM because the
            # derive passes re-read it. Everything else — the bulk — is durable
            # in Neo4j as of the enqueue above and is simply dropped.
            keep: list = []
            for e in batch:
                if e.type in defer_edge_types:
                    keep.append(e)
                elif e.type in _RETAINED_EDGE_TYPES:
                    keep.append(e.to_slim())
            return keep

    _beat("resolving", "building lookup indices")
    # A parallel-resolve branch (GRAPH_RESOLVE_WORKERS > 1, resolver_parallel.py)
    # was selected here. It required `resolve_sink is None`, but the streaming
    # writer sets a sink on every run — so the condition could never be true and
    # the only thing the knob did was log a warning and fall through to this
    # call. Removed along with the flag and the module (item #18); it also
    # degraded resolution precision at chunk boundaries, which is why
    # CONFIGURATION.md already said to keep it at 1.
    try:
        extra_nodes, resolved_edges, coverage = resolve(
            resolve_nodes, all_edges, refs_source, repo,
            on_progress=_resolve_progress, cancel_check=cancel_check,
            checkpoint_root=resolve_ckpt, edge_sink=resolve_sink,
            # When javac resolved Java's calls, the heuristic must not resolve
            # them too: its output would be discarded anyway, and producing it
            # is the expensive part (a 16.5k-file Java repo generated 3.3M
            # ambiguous CALLS at 0.1% precision). Skipping is what makes javac
            # a replacement for the work rather than an addition to it.
            skip_call_langs={"java"} if javac_owns_java else None,
        )
    finally:
        # Drain and stop the background writer before anything that assumes the
        # writes are durable (or before propagating a failure/cancellation).
        if stream_writer:
            write_queue.put(None)
            writer_thread.join()
    if stream_writer and writer_failure:
        raise writer_failure[0]
    del resolve_nodes  # just an alias for all_nodes now; drop the extra name
    all_nodes.extend(extra_nodes)
    if stream_nodes:
        late_nodes.extend(extra_nodes)  # created after the pre-resolve write
    all_edges.extend(resolved_edges)
    # all_refs is only an input to resolve() — nothing below this point reads
    # it, but it was otherwise kept alive (one RawRef per call-site/import/
    # annotation reference, comparable in count to all_edges) through SCIP,
    # dataflow, derive, and the final Neo4j write. Drop it here so it's
    # reclaimed before those whole-graph passes instead of after them.
    all_refs = []
    _mark("resolving")
    _log.info(
        "[graph_ingest][repo=%s] resolving: %s nodes, %s edges after resolve "
        "(%s retained in RAM)", repo, len(all_nodes), edge_stats.total, len(all_edges),
    )

    # Merge the javac-resolved Java CALLS produced before resolve (see the
    # javac stage above). They are full Edge objects, so the deferred-edge write
    # a few lines below picks them up and persists them like any other; nothing
    # here has to write them directly.
    #
    # No "replace what the heuristic produced" step exists, unlike the SCIP path
    # this supersedes: resolve() was told to SKIP Java CALLS refs entirely when
    # javac succeeded, so there is nothing stale to drop. That is the whole
    # reason this runs first — replacing edges after the fact is what forced
    # CALLS to be held in RAM through resolve.
    if javac_owns_java and javac_edges:
        known_node_ids.update(n.id for n in all_nodes)
        edge_stats.add(javac_edges, known_node_ids)
        all_edges.extend(javac_edges)
        coverage.pop("CALLS", None)   # javac supersedes heuristic CALLS coverage
        _log.info(
            "[graph_ingest][repo=%s] javac: merged %s type-resolved Java CALLS edge(s)",
            repo, len(javac_edges),
        )

    if on_stage:
        on_stage("deriving", {})

    # The DFG pass ran here: a SECOND full tree-sitter parse of every file with a
    # Function node, producing a per-function data-flow summary serialized onto
    # the node as dfg_json. Removed — see MEMORY_ARCHITECTURE_PLAN.md item #10.
    # Its one consumer was the analyzer's Agent B taint composition
    # (find_taint_findings), which is being retired in favour of Agent C reading
    # the source of each candidate path directly. dfg_json was a precomputed,
    # heuristic approximation of exactly what an LLM re-derives from the source
    # more accurately (it explicitly gave up on */** splats, among others), so
    # once the LLM reads the path anyway it is duplicated work.
    #
    # Cost removed: the second whole-repo parse (which dominated this stage), and
    # ~1.8KB of JSON per Function held on the node objects from here through to
    # the final write.

    # A+B rewrite Steps 1+2+11 (§11): persist whatever is still held as full
    # Edge objects — the deferred type (CALLS incl. SCIP replacements, only when
    # scip was on) — HERE, after SCIP has made CALLS final, so no edge that a
    # later stage supersedes is ever written stale. Everything else was already
    # flushed eagerly (structural edges pre-resolve; resolved edges via the
    # resolve sink) and sits in all_edges as SlimEdge stand-ins. After this write
    # the entire pre-derive edge set is durable in Neo4j and all_edges is 100%
    # slim. With scip off nothing is deferred and this write is a no-op.
    #
    # Why derive reads slim IN-RAM edges and NOT Neo4j (tried and reverted):
    # MERGE dedups relationships, so a Cypher rewrite of any pass that depends
    # on edge *multiplicity* (duplicate call sites — this used to matter for
    # fan_in/fan_out, since removed — see MEMORY_ARCHITECTURE_PLAN.md item #8)
    # would silently diverge; and _synthesize_polymorphic_calls needs
    # evidence_file/evidence_line off *existing* edges — which are not in the
    # golden edge-set hash (§7),
    # so a Cypher rewrite could regress them without the oracle even
    # noticing. The slim list preserves the full read-set (SLIM_EDGE_FIELDS)
    # exactly.
    # Edges the derive passes create below are full Edge objects, tracked
    # separately in new_edges (via _add_edges) — only they still need writing
    # at the end.
    new_edges: list[Edge] | None = None
    if stream_writer:
        pending = [e for e in all_edges if isinstance(e, Edge)]
        if pending:
            _beat("deriving", f"writing {len(pending)} deferred edge(s)")
            store.write_edges(pending, on_batch=_write_beat("deriving", "deferred edges written"))
        all_edges = [e.to_slim() if isinstance(e, Edge) else e for e in all_edges]
        new_edges = []
        _log.info(
            "[graph_ingest][repo=%s] streaming writer: wrote %s deferred edge(s) post-dataflow; "
            "all %s pre-derive edge(s) now durable in Neo4j and slim in RAM",
            repo, len(pending), len(all_edges),
        )

    def _add_edges(new: list[Edge]) -> None:
        edge_stats.add(new, known_node_ids)
        all_edges.extend(new)
        if new_edges is not None:
            new_edges.extend(new)

    # Deterministic OVERRIDES from the class hierarchy (Java + Python). SCIP gives
    # precise overrides when available for a language, so drop heuristic ones then.
    # Shared node-id / CONTAINS-parent index, built once and passed into
    # _derive_overrides instead of it rebuilding an O(nodes)/O(edges) index
    # from scratch — on a 100M+-edge graph that was a redundant full pass for
    # no behavior difference. Kept in sync with the (small) nodes/edges
    # _build_package_tree adds below; _derive_overrides never reads past a
    # File/Class ancestor so the update is behavior-preserving either way,
    # just cheap insurance against that changing later.
    _beat("deriving", "building shared node/parent index")
    shared_by_id: dict[str, Node] = {n.id: n for n in all_nodes}
    shared_parent_of: dict[str, str] = {}
    for e in all_edges:
        if e.type == "CONTAINS":
            shared_parent_of[e.dst] = e.src

    _beat("deriving", "overrides from class hierarchy")
    override_edges = _derive_overrides(all_nodes, all_edges, shared_by_id, shared_parent_of)
    # OVERRIDES is derived from EXTENDS/IMPLEMENTS + method name/arity, which the
    # heuristic resolves accurately (unlike CALLS) — javac replaces call binding
    # only, so these are kept for every language. The SCIP path used to drop them
    # per-language because SCIP supplied its own; javac does not.
    _add_edges(override_edges)

    _beat("deriving", "package tree")
    pkg_nodes, pkg_edges = _build_package_tree(all_nodes, repo)
    all_nodes.extend(pkg_nodes)
    known_node_ids.update(n.id for n in pkg_nodes)
    if stream_nodes:
        late_nodes.extend(pkg_nodes)
    _add_edges(pkg_edges)
    for n in pkg_nodes:
        shared_by_id[n.id] = n
    for e in pkg_edges:
        if e.type == "CONTAINS":
            shared_parent_of[e.dst] = e.src

    # Polymorphic-dispatch CALLS are no longer synthesized here — they are built
    # server-side after the final write (see _run_polymorphic_cypher).

    # SQL linkage: emit READS/WRITES edges from Function nodes to Table nodes
    # by scanning function source for embedded SQL patterns.  Runs last so
    # Table nodes (from .sql files) and Function nodes are both present.
    _beat("deriving", "SQL table links")
    sql_link_edges = _derive_sql_links(all_nodes, root)
    _add_edges(sql_link_edges)

    _mark("deriving")
    _log.info("[graph_ingest][repo=%s] deriving: %s nodes, %s edges before write", repo, len(all_nodes), edge_stats.total)

    # A joblib/pickle dump-the-whole-graph escape hatch lived here behind an
    # `if False:` (it dumped the resolved graph to sharded pickles and skipped
    # the Neo4j write, for reloading via load_graph_to_neo4j.py). It had been
    # dead for a while and could not be re-enabled as written: it required the
    # whole graph in memory, which the now-unconditional streaming node/edge
    # write makes impossible — its own guard raised on `stream_nodes or
    # stream_writer`, and both are hardcoded True. Removed along with
    # GRAPH_DUMP_GRAPH_PATH / GRAPH_DUMP_SHARD_SIZE.

    if on_stage:
        on_stage("writing_graph", {"nodes": len(all_nodes), "edges": edge_stats.total})
    # Extracted nodes were written (and their bulky fields dropped) before
    # resolve, and wipe/delete already ran then. Write only the nodes created
    # since (resolve's synthesized nodes + package/module). Net Neo4j state is
    # identical to a full write_nodes(all_nodes), but the full node objects were
    # never held for a second dict conversion here.
    #
    # A write_semantics() call followed, patching derived properties onto the
    # already-written nodes. With the DFG pass gone (item #10) no derive step
    # sets a node property any more — roles, call metrics and module ownership
    # were removed by items #3 and #8 — so there is nothing left to patch.
    _beat("writing_graph", f"writing {len(late_nodes)} late node(s)")
    store.write_nodes(late_nodes, on_batch=_write_beat("writing_graph", "late nodes written"))
    # all_edges is a mix of SlimEdge (pre-derive, already durable in Neo4j
    # from Step 1) and Edge (derive-added) by this point — only new_edges
    # (the latter) are real Edge objects with .props(), and only they are new
    # since Step 1's write. Writing all_edges here would both crash (SlimEdge
    # has no .props()) and redundantly re-write millions of already-written
    # edges.
    _beat("writing_graph", f"writing {len(new_edges)} derived edge(s)")
    store.write_edges(new_edges, on_batch=_write_beat("writing_graph", "derived edges written"))
    _log.info(
        "[graph_ingest][repo=%s] streaming writer: wrote %s newly-derived edge(s) "
        "(pre-derive edges already durable in Neo4j from Step 1)",
        repo, len(new_edges),
    )
    n_poly = _run_polymorphic_cypher(store, repo, edge_stats, _beat)
    _beat("writing_graph", "validating graph")
    validation = validate_graph(all_nodes, edge_stats)
    _mark("writing_graph")
    _log.info("[graph_ingest][repo=%s] writing_graph: wrote %s nodes, %s edges to Neo4j", repo, len(all_nodes), edge_stats.total)

    if manifest is not None:
        # Run succeeded end-to-end (including the Neo4j write) — the
        # checkpoint's job is done, clear it so it doesn't linger on disk or
        # get mistakenly reused by an unrelated future run of the same tag.
        clear_checkpoint(ckpt_root, repo)

    return IndexResult(
        repo=repo,
        files=total_files,
        nodes=len(all_nodes),
        edges=edge_stats.total,
        seconds=time.time() - t0,
        coverage=dict(coverage),
        validation=validation,
        db_counts=store.counts(repo),
        javac=javac_report,
        stage_seconds=stage_seconds,
    )


def _run_polymorphic_cypher(store, repo: str, edge_stats, _beat) -> int:
    """Create the polymorphic-dispatch CALLS edges in the database, after every
    CALLS and OVERRIDES is durable. Returns how many were created and folds that
    into the running tallies so validate_graph still reports the whole graph.

    Runs here rather than in derive because it needs the written graph; that is
    also what lets it replace a Python pass over the full edge set."""
    _beat("writing_graph", "polymorphic dispatch CALLS (in-database)")
    n = synthesize_polymorphic_calls_cypher(store, repo)
    edge_stats.add_count("CALLS", n)
    _log.info(
        "[graph_ingest][repo=%s] polymorphic dispatch: %s synthetic CALLS edge(s) created in-database",
        repo, n,
    )
    return n


def _derive_overrides(
    nodes: list[Node], edges: list[Edge],
    by_id: dict[str, Node] | None = None,
    parent_of: dict[str, str] | None = None,
) -> list[Edge]:
    """OVERRIDES from the resolved class hierarchy: a method overrides an
    ancestor method of the same name + arity (Java extends/implements, Python
    inheritance). Confidence INFERRED — name+arity match, not type-precise.

    ``by_id``/``parent_of``: optional precomputed node-id index and CONTAINS
    parent map, built once by index_repo's derive sequence and passed in
    instead of this function rebuilding an identical O(nodes)/O(edges) index
    from scratch. Falls back to building them locally when not provided (e.g.
    the low-RAM streaming derive path, which doesn't share this index)."""
    if by_id is None:
        by_id = {n.id: n for n in nodes}
    build_parent_of = parent_of is None
    if build_parent_of:
        parent_of = {}
    supers: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if build_parent_of and e.type == "CONTAINS":
            parent_of[e.dst] = e.src
        elif e.type in ("EXTENDS", "IMPLEMENTS"):
            s, d = by_id.get(e.src), by_id.get(e.dst)
            if s and d and s.label == "Class" and d.label == "Class":
                supers[e.src].append(e.dst)

    methods_of: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
    for n in nodes:
        if n.label == "Function" and n.kind == "method":
            cls = parent_of.get(n.id)
            if cls and by_id.get(cls) and by_id[cls].label == "Class":
                methods_of[cls][n.name].append(n)

    out: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes:
        if n.label != "Function" or n.kind != "method":
            continue
        cls = parent_of.get(n.id)
        if cls is None:
            continue
        # BFS up the ancestor chain (multiple inheritance / interfaces).
        visited: set[str] = set()
        stack = list(supers.get(cls, []))
        while stack:
            anc = stack.pop()
            if anc in visited:
                continue
            visited.add(anc)
            for m in methods_of.get(anc, {}).get(n.name, []):
                if m.id != n.id and m.param_count == n.param_count:
                    key = (n.id, m.id)
                    if key not in seen:
                        seen.add(key)
                        out.append(Edge(
                            "OVERRIDES", n.id, m.id,
                            confidence=Confidence.INFERRED.value,
                            origin=Origin.EXTRACTED.value,
                            extractor="heuristic", strategy="hierarchy",
                            evidence_file=n.file, evidence_line=n.start_line,
                        ))
            stack.extend(supers.get(anc, []))
    return out


def _build_package_tree(nodes: list[Node], repo: str) -> tuple[list[Node], list[Edge]]:
    """Materialize Repository -> Package* -> File containment from each File's
    package (Java package decl / Python directory path). Built once over all
    files so package/repo CONTAINS edges are emitted exactly once."""
    new_nodes: list[Node] = []
    new_edges: list[Edge] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()

    def add_node(nid, label, name, fqn, **kw):
        if nid in seen_nodes:
            return
        seen_nodes.add(nid)
        new_nodes.append(Node(id=nid, label=label, name=name, fqn=fqn, repo=repo, **kw))

    def add_contains(src_id, dst_id):
        if (src_id, dst_id) in seen_edges:
            return
        seen_edges.add((src_id, dst_id))
        new_edges.append(Edge(
            "CONTAINS", src_id, dst_id,
            origin=Origin.EXTRACTED.value, extractor="structure",
        ))

    repo_id = make_id(repo, repo, "repository")
    add_node(repo_id, "Repository", repo, repo, kind="repository")

    for n in nodes:
        if n.label != "File":
            continue
        parent_id = repo_id
        if n.package:
            parts = n.package.split(".")
            for i in range(len(parts)):
                fqn = ".".join(parts[:i + 1])
                pid = make_id(repo, fqn, "package")
                add_node(pid, "Package", parts[i], fqn,
                         kind="package", package=fqn, lang=n.lang)
                add_contains(parent_id, pid)
                parent_id = pid
        add_contains(parent_id, n.id)

    return new_nodes, new_edges


# SQL patterns that indicate a function reads from or writes to a table.
# Applied against raw function source (already narrowed to line range).
_SQL_WRITE_RES = [
    re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[`\"']?(\w+)[`\"']?", re.I),
    re.compile(r"\bUPDATE\s+[`\"']?(\w+)[`\"']?\s+SET\b", re.I),
    re.compile(r"\bDELETE\s+FROM\s+[`\"']?(\w+)[`\"']?", re.I),
    re.compile(r"\bMERGE\s+INTO\s+[`\"']?(\w+)[`\"']?", re.I),
]
_SQL_READ_RES = [
    re.compile(r"\bFROM\s+[`\"']?(\w+)[`\"']?(?:\s+(?:AS\s+)?\w+)?(?:\s+WHERE|\s+JOIN|\s+GROUP|\s+ORDER|\s+LIMIT|[,\s]|$)", re.I),
    re.compile(r"\bJOIN\s+[`\"']?(\w+)[`\"']?", re.I),
]


def _derive_sql_links(
    all_nodes: list[Node],
    root: str,
) -> list[Edge]:
    """Emit READS/WRITES edges from Function nodes to Table nodes by scanning
    function source text for embedded SQL patterns.

    Only emits edges when both a Function and a matching Table node exist in
    all_nodes (same repo).  This runs at index time, so no DB query is needed.

    ``root``: repo root — source is read fresh from disk per matching
    function here (instead of requiring every file to already be cached in
    memory from an earlier discover() pass), so this step's memory footprint
    is bounded by the (typically small) subset of files with a Table match,
    not the whole repo's file count.
    """
    # Build table lookup: lowercase table name -> table node id (per repo)
    tables_by_repo: dict[str, dict[str, str]] = {}
    for n in all_nodes:
        if n.label == "Table":
            tables_by_repo.setdefault(n.repo, {})[n.name.lower()] = n.id

    if not tables_by_repo:
        return []

    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    _source_cache: dict[str, bytes] = {}

    def _read_source(relpath: str) -> bytes | None:
        if relpath in _source_cache:
            return _source_cache[relpath]
        try:
            with open(os.path.join(root, *relpath.split("/")), "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
        _source_cache[relpath] = data
        return data

    for fn in all_nodes:
        if fn.label != "Function" or not fn.file or not fn.start_line:
            continue
        known_tables = tables_by_repo.get(fn.repo)
        if not known_tables:
            continue
        source = _read_source(fn.file)
        if source is None:
            continue

        src_lines = source.decode("utf-8", "replace").splitlines()
        start = max(0, fn.start_line - 1)
        end = fn.end_line if fn.end_line else len(src_lines)
        fn_src = "\n".join(src_lines[start:end])

        for pat in _SQL_WRITE_RES:
            for m in pat.finditer(fn_src):
                tname = m.group(1).lower()
                tid = known_tables.get(tname)
                if tid:
                    k = (fn.id, "WRITES", tid)
                    if k not in seen:
                        seen.add(k)
                        edges.append(Edge(
                            type="WRITES",
                            src=fn.id,
                            dst=tid,
                            confidence=Confidence.INFERRED.value,
                            origin=Origin.DERIVED.value,
                            extractor="sql_linker",
                            evidence_file=fn.file,
                            evidence_line=fn.start_line,
                        ))

        for pat in _SQL_READ_RES:
            for m in pat.finditer(fn_src):
                tname = m.group(1).lower()
                tid = known_tables.get(tname)
                if tid:
                    k = (fn.id, "READS", tid)
                    if k not in seen:
                        seen.add(k)
                        edges.append(Edge(
                            type="READS",
                            src=fn.id,
                            dst=tid,
                            confidence=Confidence.INFERRED.value,
                            origin=Origin.DERIVED.value,
                            extractor="sql_linker",
                            evidence_file=fn.file,
                            evidence_line=fn.start_line,
                        ))

    return edges

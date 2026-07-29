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
from .checkpoint import RefStream, batch_exists, clear_checkpoint, init_manifest, load_batch, save_batch
from .config import checkpoint_root as get_checkpoint_root
from .config import (
    extract_batch_size,
    extract_worker_count,
    get_cache_io_workers,
    get_dump_graph_path,
    get_dump_shard_size,
    get_edge_spill_dir,
    get_lowram_derive,
    get_resolve_workers,
    is_streaming_ingest_enabled,
)
from .dataflow import DataflowResult, run_dataflow
from .discovery import FileInfo, discover, list_candidate_relpaths
from .extract_cache import get_extract_cache
from .ids import make_id
from .models import Confidence, Edge, Node, Origin, RawRef, _clean
from .resolver import Coverage, resolve
from .resolver_parallel import parallel_resolve
from .scip_resolver import (
    ScipReport,
    finish_scip_java_job,
    scip_resolve,
    start_scip_java_job,
)
from .store import GraphStore
from .validator import validate_graph
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

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
    scip: ScipReport = field(default_factory=ScipReport)
    scip_java: ScipReport = field(default_factory=ScipReport)
    roles: dict = field(default_factory=dict)  # (role, source) -> count diagnostics
    dfg: DataflowResult = field(default_factory=DataflowResult)
    stage_seconds: dict[str, float] = field(default_factory=dict)  # per-stage timing breakdown


def index_repo(root: str, repo: str, store: GraphStore, wipe: bool = True,
               scip: bool = True, on_stage: OnStage = None,
               candidate_files: list[str] | None = None,
               cancel_check: Callable[[], bool] | None = None,
               changed_files: list[str] | None = None,
               deleted_files: list[str] | None = None) -> IndexResult:
    """``changed_files``/``deleted_files``: incremental-ingest mode. When either
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
        pre-resolve node write, resolver index build, dataflow, and each derive
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

    # Kick off scip-java's compile as early as possible — it's a subprocess
    # over the raw repo files, independent of anything extraction produces,
    # so it can run in the background while the (CPU-bound) extraction loop
    # below does its work instead of waiting until after resolve() as before.
    has_java = any(p.lower().endswith(".java") for p in all_relpaths)
    java_job = None
    if scip and has_java:
        java_file_count = sum(1 for p in all_relpaths if p.lower().endswith(".java"))
        java_job = start_scip_java_job(root, java_file_count)

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
    # Two edge types must be DEFERRED (held full, written only at the
    # post-dataflow write point) because a later stage rewrites them in RAM and
    # an eager write would leave stale rows in Neo4j:
    #   - PASSES: dataflow drops every pre-dataflow PASSES edge and emits its
    #     own (they're never written pre-dataflow on the legacy path either);
    #   - CALLS, only when SCIP may run: scip_python/scip_java replace whole
    #     languages' CALLS post-resolve. With scip off (the common/prod path)
    #     CALLS flush eagerly like everything else.
    stream_writer = True
    defer_edge_types = {"PASSES"} | ({"CALLS"} if scip else set())
    streamed_ref_count = 0
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
    # RAM (none are read after extraction by resolve/scip/dataflow/derive — see
    # MEMORY_ARCHITECTURE_PLAN.md item #1 for the full field-by-field audit;
    # notably end_line/param_names/body_hash/repo are NOT cleared here — they
    # ARE read downstream, by scip_resolver.py/dataflow.py/_derive_sql_links).
    # Neo4j is their source of truth from here. Wipe/delete must precede this
    # early write. Nodes created later (resolve's synthesized nodes, package
    # nodes) are tracked in late_nodes and written at the end; derived
    # properties (call metrics, DFG) are patched onto the already-written
    # nodes at the end via write_semantics, yielding the same final Neo4j
    # state as one full write.
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
        for n in all_nodes:
            n.docstring = ""
            n.signature = ""
            n.param_types = []
            n.return_type = ""
            n.end_col = 0
            n.display_name = ""
            n.visibility = ""
            n.modifiers = []
            n.is_static = False
            n.is_abstract = False
            n.is_async = False
            n.host = ""
            n.loc = 0
            n.cyclomatic = 0
            n.branch_count = 0
            n.loop_count = 0
            n.is_lock = False
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
    if is_streaming_ingest_enabled():
        resolve_nodes = [n.to_slim() for n in all_nodes]
        _log.info(
            "[graph_ingest][repo=%s] streaming ingest: resolving against slim projection of %s node(s)%s",
            repo, len(resolve_nodes), " (refs streamed from disk)" if stream_refs else "",
        )
    else:
        resolve_nodes = all_nodes
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
            to_write = [e for e in batch if e.type not in defer_edge_types]
            if new_nodes or to_write:
                _enqueue_write(new_nodes, to_write)
            return [e if e.type in defer_edge_types else e.to_slim() for e in batch]

    # --- Option A: low-RAM derive setup (GRAPH_LOWRAM_DERIVE) ---------------
    # Sink resolve's produced edges (the 100M+ CALLS etc.) straight to disk so
    # they never accumulate in RAM — this is what kills the resolve peak. The
    # small STRUCTURAL edges stay in RAM (resolve needs CONTAINS for its scope
    # chain, and the derive passes need them for random access). PASSES are
    # dropped here (dataflow regenerates them) — matching the normal path's
    # `[e for e in all_edges if e.type != "PASSES"]`.
    lowram = get_lowram_derive()
    lowram_disk = None
    if lowram:
        if stream_writer or is_streaming_ingest_enabled():
            raise RuntimeError(
                "GRAPH_LOWRAM_DERIVE is standalone — turn OFF streaming ingest/writer "
                "(GRAPH_STREAMING_INGEST / GRAPH_STREAMING_WRITER)."
            )
        if scip:
            raise RuntimeError("GRAPH_LOWRAM_DERIVE does not support SCIP yet; set GRAPH_SCIP_ENABLED=false.")
        if incremental:
            raise RuntimeError("GRAPH_LOWRAM_DERIVE supports full (wipe) ingest only, not incremental.")
        from .lowram_derive import DiskEdgeStore
        _LOWRAM_STRUCT_TYPES = {"CONTAINS", "EXTENDS", "IMPLEMENTS", "ANNOTATED_WITH", "EXPOSES"}
        spill_dir = os.path.join(get_edge_spill_dir(), repo)
        import shutil as _shutil
        _shutil.rmtree(spill_dir, ignore_errors=True)  # never reuse a stale spill
        lowram_disk = DiskEdgeStore(spill_dir)
        _struct_edges = [e for e in all_edges if e.type in _LOWRAM_STRUCT_TYPES]
        for e in all_edges:
            if e.type not in _LOWRAM_STRUCT_TYPES and e.type != "PASSES":
                lowram_disk.append(e)
        _log.info(
            "[graph_ingest][repo=%s] lowram: %s structural edge(s) kept in RAM, %s bulk edge(s) spilled to %s",
            repo, len(_struct_edges), len(lowram_disk), spill_dir,
        )
        all_edges = _struct_edges  # resolve reads structural for parent_of; bulk is on disk

        def resolve_sink(new_nodes: list[Node], batch: list[Edge]) -> list:
            # extra_nodes are returned by resolve() in full and added below, so
            # new_nodes here can be ignored; retain nothing (return []) so resolve
            # holds no edges — they live only on disk.
            lowram_disk.extend(batch)
            return []

    _beat("resolving", "building lookup indices")
    resolve_workers = get_resolve_workers()
    use_parallel_resolve = (
        resolve_workers > 1 and resolve_sink is None and isinstance(refs_source, list)
    )
    if resolve_workers > 1 and not use_parallel_resolve:
        _log.warning(
            "[graph_ingest][repo=%s] GRAPH_RESOLVE_WORKERS=%s requested but not usable here "
            "(streaming writer active and/or refs are disk-streamed) — falling back to "
            "sequential resolve()", repo, resolve_workers,
        )
    try:
        if use_parallel_resolve:
            extra_nodes, resolved_edges, coverage = parallel_resolve(
                resolve_nodes, all_edges, refs_source, repo, resolve_workers,
                on_progress=_resolve_progress, cancel_check=cancel_check,
            )
        else:
            extra_nodes, resolved_edges, coverage = resolve(
                resolve_nodes, all_edges, refs_source, repo,
                on_progress=_resolve_progress, cancel_check=cancel_check,
                checkpoint_root=resolve_ckpt, edge_sink=resolve_sink,
            )
    finally:
        # Drain and stop the background writer before anything that assumes the
        # writes are durable (or before propagating a failure/cancellation).
        if stream_writer:
            write_queue.put(None)
            writer_thread.join()
    if stream_writer and writer_failure:
        raise writer_failure[0]
    del resolve_nodes  # slim projection is dead after resolve; free it promptly
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
    _log.info("[graph_ingest][repo=%s] resolving: %s nodes, %s edges after resolve", repo, len(all_nodes), len(all_edges))

    # Option A: low-RAM derive+write takes over here (bulk edges are on disk;
    # all_edges holds only structural). Skips SCIP + the in-RAM tail entirely.
    if lowram:
        return _lowram_derive_and_write(
            store, all_nodes, all_edges, lowram_disk, root, repo, coverage,
            wipe, total_files, t0, stage_seconds, on_stage, _beat, _mark, _write_beat,
        )

    # Stage 2 (precise): let SCIP own Python CALLS — type-precise and cross-file.
    # The heuristic keeps non-Python CALLS (e.g. Java, where scip-java needs a
    # build tool) plus every other edge type.
    scip_report = ScipReport()
    has_python = any(p.lower().endswith(".py") for p in all_relpaths)
    lang_of = {n.id: n.lang for n in all_nodes}
    scip_owns_python = False
    if scip and has_python:
        if on_stage:
            on_stage("scip_python", {})
        scip_edges, scip_report = scip_resolve(all_nodes, root, repo)
        if scip_report.available:
            scip_owns_python = True
            all_edges = [
                e for e in all_edges
                if not (e.type == "CALLS" and lang_of.get(e.src) == "python")
            ]
            all_edges.extend(scip_edges)
            coverage.pop("CALLS", None)  # superseded by SCIP for Python
    _mark("scip_python")

    # Same idea for Java via scip-java, gated on an actual Maven/Gradle build
    # being present (scip-java compiles the project with a semanticdb-javac
    # plugin to get type info, so it can't run without one). Falls back to the
    # (package/import-aware) heuristic resolver on any failure — missing
    # binary, no JDK, no network to fetch deps, compile errors, etc. The
    # subprocess itself was already started above (before extraction); this
    # just joins it, so most of its compile time overlaps with the extraction
    # loop instead of running fully after it.
    scip_java_report = ScipReport()
    scip_owns_java = False
    if java_job is not None:
        def _java_heartbeat(elapsed: float, timeout: float) -> None:
            if on_stage:
                on_stage("scip_java", {"elapsed_s": elapsed, "timeout_s": timeout})
        if on_stage:
            on_stage("scip_java", {"elapsed_s": 0.0, "timeout_s": java_job.timeout})
        scip_java_edges, scip_java_report = finish_scip_java_job(
            all_nodes, java_job, repo, on_heartbeat=_java_heartbeat,
        )
        if scip_java_report.available:
            scip_owns_java = True
            all_edges = [
                e for e in all_edges
                if not (e.type == "CALLS" and lang_of.get(e.src) == "java")
            ]
            all_edges.extend(scip_java_edges)
            coverage.pop("CALLS", None)  # superseded by SCIP for Java (and/or Python above)
    _mark("scip_java")

    if on_stage:
        on_stage("deriving", {})

    # DFG: compute function-summary data-flow graph and PASSES edges.
    # Must run after SCIP so CALLS edges are as precise as possible (needed for
    # binding ArgFlows to callee nodes). Replaces extractor-emitted PASSES edges
    # so the parallel-array flow properties are always present.
    _beat("deriving", "dataflow: computing function summaries + PASSES edges")
    dfg_result = run_dataflow(root, all_nodes, all_edges, repo)
    all_edges = [e for e in all_edges if e.type != "PASSES"]
    all_edges.extend(dfg_result.passes_edges)
    nodes_by_id = {n.id: n for n in all_nodes}
    for fn_id, props in dfg_result.node_props.items():
        fn_node = nodes_by_id.get(fn_id)
        if fn_node is not None:
            fn_node.dfg_json = props.get("dfg_json", "")
            fn_node.dfg_returns_from_params = props.get("dfg_returns_from_params", [])
            fn_node.dfg_hash = props.get("dfg_hash", "")
    # node_props/passes_edges are now fully applied to all_nodes/all_edges —
    # only .stats is ever read from the result (see IndexResult.dfg below), so
    # drop the rest here instead of holding it (one dfg_json string per
    # function, duplicated) through derive + write.
    dfg_stats = dfg_result.stats
    del dfg_result

    # A+B rewrite Steps 1+2+11 (§11): persist whatever is still held as full
    # Edge objects — the deferred types (PASSES from dataflow just above;
    # CALLS incl. SCIP replacements when scip was on) — HERE, after SCIP
    # (CALLS final) and dataflow (PASSES final), so no edge that a later stage
    # supersedes is ever written stale. Everything else was already flushed
    # eagerly (structural edges pre-resolve; resolved edges via the resolve
    # sink) and sits in all_edges as SlimEdge stand-ins. After this write the
    # entire pre-derive edge set is durable in Neo4j and all_edges is 100%
    # slim.
    #
    # Why derive reads slim IN-RAM edges and NOT Neo4j (tried and reverted):
    # MERGE dedups relationships, so a Cypher rewrite of any pass that depends
    # on edge *multiplicity* (duplicate call sites — this used to matter for
    # fan_in/fan_out, since removed — see MEMORY_ARCHITECTURE_PLAN.md item #8)
    # would silently diverge; and _synthesize_polymorphic_calls needs
    # evidence_file/evidence_line while dataflow needs confidence off
    # *existing* edges — none of which are in the golden edge-set hash (§7),
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
    if scip_owns_python:
        override_edges = [e for e in override_edges if lang_of.get(e.src) != "python"]
    if scip_owns_java:
        override_edges = [e for e in override_edges if lang_of.get(e.src) != "java"]
    _add_edges(override_edges)

    _beat("deriving", "package tree")
    pkg_nodes, pkg_edges = _build_package_tree(all_nodes, repo)
    all_nodes.extend(pkg_nodes)
    if stream_nodes:
        late_nodes.extend(pkg_nodes)
    _add_edges(pkg_edges)
    for n in pkg_nodes:
        shared_by_id[n.id] = n
    for e in pkg_edges:
        if e.type == "CONTAINS":
            shared_parent_of[e.dst] = e.src

    # Synthesize polymorphic-dispatch CALLS edges (reads the OVERRIDES edges
    # just derived above) before every downstream CALLS-based caller query.
    # Reads evidence_file/evidence_line off existing CALLS edges — carried by
    # the slim projection (SLIM_EDGE_FIELDS) even though it's outside the
    # golden edge-set hash.
    _beat("deriving", "polymorphic dispatch CALLS")
    _add_edges(_synthesize_polymorphic_calls(all_edges))

    # SQL linkage: emit READS/WRITES edges from Function nodes to Table nodes
    # by scanning function source for embedded SQL patterns.  Runs last so
    # Table nodes (from .sql files) and Function nodes are both present.
    _beat("deriving", "SQL table links")
    sql_link_edges = _derive_sql_links(all_nodes, root)
    _add_edges(sql_link_edges)

    _beat("deriving", "validating graph")
    validation = validate_graph(all_nodes, all_edges)
    _mark("deriving")
    _log.info("[graph_ingest][repo=%s] deriving: %s nodes, %s edges before write", repo, len(all_nodes), len(all_edges))

    # --- JOBLIB DUMP possibility (DISABLED — kept for re-enabling) ---------
    # Dumps the fully resolved+derived graph to a sharded pickle and SKIPS the
    # Neo4j write, so it can be loaded separately via load_graph_to_neo4j.py
    # (retune batch size / workers / target Aura without redoing resolve).
    # To re-enable: change `if False:` below back to `if dump_path:`, and
    # re-enable --dump-graph in cli.py + the dump field in ui/app.py.
    dump_path = get_dump_graph_path()
    if False:  # was: if dump_path:
        if stream_nodes or stream_writer:
            raise RuntimeError(
                "GRAPH_DUMP_GRAPH_PATH is set but streaming ingest/writer is enabled. "
                "Dump mode needs the full graph in memory (streaming drops node text "
                "and writes edges to Neo4j during resolve, leaving no single final graph "
                "to dump). Re-run with streaming ingest AND streaming writer OFF."
            )
        import pickle
        parent = os.path.dirname(os.path.abspath(dump_path))
        os.makedirs(parent, exist_ok=True)
        # Streamed, sharded dump: header + nodes + N edge shards, each a separate
        # pickle frame in one file. The loader reads it frame-by-frame so it never
        # holds all edges in RAM at once (essential at 100M+ edges). Nodes are far
        # fewer, written as a single frame.
        shard = get_dump_shard_size()
        n_edges = len(all_edges)
        n_shards = (n_edges + shard - 1) // shard if n_edges else 0
        _beat(
            "writing_graph",
            f"dumping {len(all_nodes)} node(s) + {n_edges} edge(s) to {dump_path} "
            f"in {n_shards} shard(s) of {shard}",
        )
        with open(dump_path, "wb") as fh:
            pickle.dump(
                {
                    "format": "sharded-v1",
                    "repo": repo,
                    "n_nodes": len(all_nodes),
                    "n_edges": n_edges,
                    "shard_size": shard,
                    "n_shards": n_shards,
                },
                fh, protocol=pickle.HIGHEST_PROTOCOL,
            )
            pickle.dump(all_nodes, fh, protocol=pickle.HIGHEST_PROTOCOL)
            for i in range(0, n_edges, shard):
                pickle.dump(all_edges[i:i + shard], fh, protocol=pickle.HIGHEST_PROTOCOL)
                _beat("writing_graph", f"dumped edge shard, {min(i + shard, n_edges)}/{n_edges}")
        _mark("writing_graph")
        _log.info(
            "[graph_ingest][repo=%s] dumped resolved graph to %s (%s node(s), %s edge(s), "
            "%s shard(s); skipped Neo4j write)",
            repo, dump_path, len(all_nodes), n_edges, n_shards,
        )
        if manifest is not None:
            clear_checkpoint(ckpt_root, repo)
        return IndexResult(
            repo=repo,
            files=total_files,
            nodes=len(all_nodes),
            edges=len(all_edges),
            seconds=time.time() - t0,
            coverage=dict(coverage),
            validation=validation,
            db_counts=(0, 0),
            scip=scip_report,
            scip_java=scip_java_report,
            roles={},
            dfg=DataflowResult(stats=dfg_stats),
            stage_seconds=stage_seconds,
        )

    if on_stage:
        on_stage("writing_graph", {"nodes": len(all_nodes), "edges": len(all_edges)})
    # Extracted nodes were written (and their bulky fields dropped) before
    # resolve, and wipe/delete already ran then. Write only the nodes created
    # since (resolve's synthesized nodes + package/module), then patch the
    # derived properties (roles, call metrics, module ownership, DFG) onto the
    # already-written nodes. Net Neo4j state is identical to a full
    # write_nodes(all_nodes), but the full node objects were never held for a
    # second dict conversion here.
    _beat("writing_graph", f"writing {len(late_nodes)} late node(s)")
    store.write_nodes(late_nodes, on_batch=_write_beat("writing_graph", "late nodes written"))
    _beat("writing_graph", "patching derived node properties")
    store.write_semantics(_derived_semantics_rows(all_nodes))
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
    _mark("writing_graph")
    _log.info("[graph_ingest][repo=%s] writing_graph: wrote %s nodes, %s edges to Neo4j", repo, len(all_nodes), len(all_edges))

    if manifest is not None:
        # Run succeeded end-to-end (including the Neo4j write) — the
        # checkpoint's job is done, clear it so it doesn't linger on disk or
        # get mistakenly reused by an unrelated future run of the same tag.
        clear_checkpoint(ckpt_root, repo)

    return IndexResult(
        repo=repo,
        files=total_files,
        nodes=len(all_nodes),
        edges=len(all_edges),
        seconds=time.time() - t0,
        coverage=dict(coverage),
        validation=validation,
        db_counts=store.counts(repo),
        scip=scip_report,
        scip_java=scip_java_report,
        roles={},
        dfg=DataflowResult(stats=dfg_stats),
        stage_seconds=stage_seconds,
    )


def _lowram_derive_and_write(
    store, all_nodes, struct_edges, disk, root, repo, coverage,
    wipe, total_files, t0, stage_seconds, on_stage, _beat, _mark, _write_beat,
):
    """Option A tail: dataflow -> derive -> write, streaming the bulk edges from
    ``disk`` (a DiskEdgeStore) instead of holding them in RAM.

    RAM stays ~= nodes + structural edges + derived edges + O(nodes) accumulators.
    Reuses the in-RAM derive functions unchanged over a streamed edge view
    (ChainedEdges); only polymorphic-dispatch needs the streamed variant (the
    in-RAM one builds an all-CALLS set). Structural edges (CONTAINS/EXTENDS/…)
    and derived edges (USES/BELONGS_TO/synthetic CALLS/READS/WRITES) are moderate
    and kept in RAM; only CALLS + PASSES live on disk.
    """
    from .lowram_derive import ChainedEdges, streaming_polymorphic_calls

    if on_stage:
        on_stage("deriving", {})

    # Dataflow — streams edges (disk holds no PASSES; dataflow regenerates them).
    _beat("deriving", "dataflow: function summaries + PASSES edges")
    dfg_result = run_dataflow(root, all_nodes, ChainedEdges(struct_edges, disk), repo)
    disk.extend(dfg_result.passes_edges)
    nodes_by_id = {n.id: n for n in all_nodes}
    for fn_id, props in dfg_result.node_props.items():
        fn_node = nodes_by_id.get(fn_id)
        if fn_node is not None:
            fn_node.dfg_json = props.get("dfg_json", "")
            fn_node.dfg_returns_from_params = props.get("dfg_returns_from_params", [])
            fn_node.dfg_hash = props.get("dfg_hash", "")
    dfg_stats = dfg_result.stats
    del dfg_result

    derived: list[Edge] = []

    # Overrides — needs CONTAINS/EXTENDS/IMPLEMENTS (all structural, in RAM).
    # OVERRIDES feed module-USES (base_dep_types) and polymorphic, so they go
    # back into the structural RAM list.
    _beat("deriving", "overrides from class hierarchy")
    struct_edges.extend(_derive_overrides(all_nodes, struct_edges))

    _beat("deriving", "package tree")
    pkg_nodes, pkg_edges = _build_package_tree(all_nodes, repo)
    all_nodes.extend(pkg_nodes)
    struct_edges.extend(pkg_edges)  # package CONTAINS

    _beat("deriving", "polymorphic dispatch CALLS")
    derived.extend(streaming_polymorphic_calls(struct_edges, disk))

    _beat("deriving", "SQL table links")
    derived.extend(_derive_sql_links(all_nodes, root))

    _beat("deriving", "validating graph")
    validation = validate_graph(all_nodes, ChainedEdges(struct_edges, disk, derived))
    _mark("deriving")

    total_edges = len(struct_edges) + len(derived) + len(disk)
    if on_stage:
        on_stage("writing_graph", {"nodes": len(all_nodes), "edges": total_edges})
    store.bootstrap()
    if wipe:
        store.wipe(repo)
    _beat("writing_graph", f"writing {len(all_nodes)} node(s)")
    store.write_nodes(all_nodes, on_batch=_write_beat("writing_graph", "nodes written"))
    _beat("writing_graph", f"writing {len(struct_edges)} structural edge(s)")
    store.write_edges(struct_edges, on_batch=_write_beat("writing_graph", "structural edges written"))
    _beat("writing_graph", f"writing {len(derived)} derived edge(s)")
    store.write_edges(derived, on_batch=_write_beat("writing_graph", "derived edges written"))
    # Bulk edges: write one shard at a time so they never all sit in RAM again.
    _beat("writing_graph", f"writing {len(disk)} bulk edge(s) from disk, shard by shard")
    n_bulk = 0
    for shard in disk.shards():
        store.write_edges(shard)
        n_bulk += len(shard)
        _beat("writing_graph", f"bulk edges written {n_bulk}/{len(disk)}")
    _mark("writing_graph")
    _log.info("[graph_ingest][repo=%s] lowram writing_graph: wrote %s nodes, %s edges to Neo4j", repo, len(all_nodes), total_edges)

    import shutil as _shutil
    _shutil.rmtree(disk._dir, ignore_errors=True)  # spill served its purpose

    return IndexResult(
        repo=repo,
        files=total_files,
        nodes=len(all_nodes),
        edges=total_edges,
        seconds=time.time() - t0,
        coverage=dict(coverage),
        validation=validation,
        db_counts=store.counts(repo),
        scip=ScipReport(),
        scip_java=ScipReport(),
        roles={},
        dfg=DataflowResult(stats=dfg_stats),
        stage_seconds=stage_seconds,
    )


def _derived_semantics_rows(nodes: list[Node]) -> list[dict]:
    """Build write_semantics rows carrying only the fields the derive/dataflow
    passes SET on nodes (DFG). component_role/module_id are no longer derived
    — see MEMORY_ARCHITECTURE_PLAN.md item #3; fan_in/fan_out/recursive are no
    longer derived either — see item #8 (confirmed zero downstream consumers).

    Under streaming ingest the extracted nodes are written to Neo4j before those
    passes run, so their derived properties are patched on afterwards. Uses the
    same ``_clean`` as ``Node.props()`` (drops empty/zero/false values) so the
    patched Neo4j state matches exactly what a single full ``write_nodes`` would
    have written — nodes with no derived properties contribute no row."""
    rows: list[dict] = []
    for n in nodes:
        props = _clean({
            "dfg_json": n.dfg_json,
            "dfg_returns_from_params": n.dfg_returns_from_params,
            "dfg_hash": n.dfg_hash,
        })
        if props:
            rows.append({"id": n.id, "props": props})
    return rows


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


# Cap on existing callers of an ancestor method fanned out to each concrete
# override, so a popular interface (many implementors x many callers) can't
# mint an unbounded number of synthetic edges.
_POLY_FANOUT_GUARD = 25


def _synthesize_polymorphic_calls(edges: list[Edge]) -> list[Edge]:
    """Make callers of an ancestor method visible as callers of every concrete
    override (OVERRIDES edges, from `_derive_overrides` or SCIP), so every
    CALLS-based caller query (`_batch_callers`/`_batch_hop2_summaries` in
    analyzer/agents.py, the caller lookups in analyzer/taint.py and
    analyzer/exception_walk.py) finds real callers that reach a concrete
    override only through a supertype- or interface-typed reference — e.g.
    `Animal a = new Dog(); a.speak();` should surface as a caller of
    `Dog.speak()`, not just of `Animal.speak()`.

    One-directional only: child -> ancestor -> ancestor's existing callers.
    Does not propagate sibling-to-sibling (e.g. attributing `Cat.speak()`'s
    callers to `Dog.speak()` because both implement `Animal`) — that would be
    a new, broader false-positive source, not the gap being closed here.

    Tagged `Confidence.AMBIGUOUS` / `strategy="polymorphic_dispatch"` —
    genuinely multiple/uncertain resolution (the call could reach any
    override at runtime depending on the concrete object type). No CALLS-
    based query in `analyzer/` filters on edge confidence, so this flows
    through every existing and future caller-query unfiltered.
    """
    overrides_by_ancestor: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.type == "OVERRIDES":
            overrides_by_ancestor[e.dst].append(e.src)
    if not overrides_by_ancestor:
        return []

    # The dedup check below only ever probes (caller.src, child) where child
    # is a concrete override — so only CALLS edges landing on a concrete
    # override can ever match. Previously this built `existing_calls` from
    # *every* CALLS edge in the graph (100M+ on a large repo) just to guard
    # against duplicates on this much smaller set of pairs. Narrowing it to
    # calls-into-children only (bounded by calls-into-overrides, not total
    # calls) and building it in the same pass as callers_by_ancestor — instead
    # of two separate full scans — mirrors streaming_polymorphic_calls in
    # lowram_derive.py (unit-tested byte-identical output; see test there).
    children_set = {c for children in overrides_by_ancestor.values() for c in children}
    callers_by_ancestor: dict[str, list[Edge]] = defaultdict(list)
    existing_into_children: set[tuple[str, str]] = set()
    for e in edges:
        if e.type != "CALLS":
            continue
        if e.dst in overrides_by_ancestor:
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

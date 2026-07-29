"""Per-batch extraction checkpoints — crash-resume + extraction-phase memory
offload for index_repo()'s discover+extract loop.

Design (see pipeline.py's index_repo for the caller side):
    <checkpoint_root>/<safe_repo_tag>/
        manifest.json      {"total_batches": N, "batch_size": B, "batches_done": [0,1,...]}
        batch_0000.joblib  CanonicalBundle (nodes, edges, refs) for that batch
        batch_0001.joblib
        ...

Only affects WHEN/WHERE extracted graph data lives in memory during
discover+extract — does not change extraction or resolution logic/results.
A batch is only ever (re-)processed if it is not already marked done in the
manifest, so a retried run of the same repo tag (same job_id) can skip
already-completed batches instead of restarting extraction from file 1.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from queue import Full, Queue

import joblib

from .canonical_ir import CanonicalBundle
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_MANIFEST_NAME = "manifest.json"
_SAFE_TAG_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_tag(repo_tag: str) -> str:
    """Filesystem-safe directory name for a repo tag (e.g. 'upload:<job_id>')."""
    return _SAFE_TAG_RE.sub("_", repo_tag)


def checkpoint_dir_for(checkpoint_root: str, repo_tag: str) -> str:
    return os.path.join(checkpoint_root, _safe_tag(repo_tag))


def load_manifest(checkpoint_root: str, repo_tag: str) -> dict | None:
    """Return the existing manifest for this repo tag, or None if absent/corrupt."""
    path = os.path.join(checkpoint_dir_for(checkpoint_root, repo_tag), _MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("batches_done"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_manifest(checkpoint_root: str, repo_tag: str, manifest: dict) -> None:
    d = checkpoint_dir_for(checkpoint_root, repo_tag)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _MANIFEST_NAME)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    os.replace(tmp_path, path)  # atomic on POSIX — avoids a half-written manifest on crash


def init_manifest(checkpoint_root: str, repo_tag: str, total_batches: int, batch_size: int) -> dict:
    """Load an existing manifest matching this run's shape, or start a fresh one.

    If a prior manifest exists but total_batches/batch_size differ (e.g. the
    repo content or batch-size config changed between runs), the stale
    checkpoint is discarded rather than risking an incorrect resume.
    """
    existing = load_manifest(checkpoint_root, repo_tag)
    if existing and existing.get("total_batches") == total_batches and existing.get("batch_size") == batch_size:
        _log.info(
            "[graph_checkpoint][repo=%s] resuming: %s/%s batches already done",
            repo_tag, len(existing.get("batches_done", [])), total_batches,
        )
        return existing
    if existing:
        _log.warning(
            "[graph_checkpoint][repo=%s] discarding stale checkpoint (shape changed)", repo_tag,
        )
        clear_checkpoint(checkpoint_root, repo_tag)
    manifest = {"total_batches": total_batches, "batch_size": batch_size, "batches_done": []}
    _write_manifest(checkpoint_root, repo_tag, manifest)
    return manifest


def save_batch(checkpoint_root: str, repo_tag: str, batch_index: int, bundle: CanonicalBundle, manifest: dict) -> None:
    """Persist one batch's merged bundle to disk and mark it done in the manifest."""
    d = checkpoint_dir_for(checkpoint_root, repo_tag)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"batch_{batch_index:04d}.joblib")
    tmp_path = path + ".tmp"
    joblib.dump(bundle, tmp_path)
    os.replace(tmp_path, path)  # atomic — a crash mid-write never leaves a partial batch file
    if batch_index not in manifest["batches_done"]:
        manifest["batches_done"].append(batch_index)
    _write_manifest(checkpoint_root, repo_tag, manifest)


def batch_exists(checkpoint_root: str, repo_tag: str, batch_index: int) -> bool:
    """Cheap existence check — no deserialization — for deciding whether a
    batch marked done in the manifest is safe to resume from.

    ``save_batch`` writes atomically (dump to a .tmp file, then ``os.replace``
    to the final name), so the final name existing means that write fully
    completed; a crash mid-write leaves only the .tmp file behind, never a
    corrupt final file. That covers the actual resume scenario (a batch that
    never finished writing) without paying load_batch's full joblib
    deserialization cost just to immediately discard the result — the one
    place that data is actually needed (the post-loop reassembly pass in
    pipeline.py) already calls load_batch for real and would itself skip a
    batch that somehow fails to deserialize despite existing (bit rot,
    manual tampering) — an unrelated, much rarer case this check isn't
    trying to catch."""
    path = os.path.join(checkpoint_dir_for(checkpoint_root, repo_tag), f"batch_{batch_index:04d}.joblib")
    return os.path.isfile(path)


def load_batch(checkpoint_root: str, repo_tag: str, batch_index: int) -> CanonicalBundle | None:
    path = os.path.join(checkpoint_dir_for(checkpoint_root, repo_tag), f"batch_{batch_index:04d}.joblib")
    try:
        return joblib.load(path)
    except (OSError, EOFError, ValueError) as exc:
        _log.warning("[graph_checkpoint][repo=%s] failed to load batch %s: %s", repo_tag, batch_index, exc)
        return None


def clear_checkpoint(checkpoint_root: str, repo_tag: str) -> None:
    """Remove all checkpoint data for this repo tag (called after a successful
    run, or when discarding a stale/shape-mismatched checkpoint)."""
    d = checkpoint_dir_for(checkpoint_root, repo_tag)
    shutil.rmtree(d, ignore_errors=True)


class RefStream:
    """Memory-bounded iterable over every RawRef across a repo's extraction
    checkpoint batches (see TIER3_MEMORY_PLAN.md Phase 3).

    Loads and discards one batch file at a time, so the full ref list (millions
    on a large repo — the single biggest item held during resolve) is never
    materialized in memory at once. The caller (resolve) iterates it twice — once
    to build the import indices, once to resolve — each pass re-reading the batch
    files from disk; that extra I/O is the deliberate trade for bounded memory.

    ``__len__`` is precomputed by the caller during reassembly (it already loads
    every batch there for nodes/edges, so ref counts come for free); if not
    provided it is counted lazily via one extra pass.

    Within-batch dedup already happened when each batch was saved
    (``save_batch`` persists the merged bundle), and a file never spans two
    batches, so cross-batch ref duplication does not occur — streaming the
    per-batch refs yields exactly the same ref set the old single ``all_refs``
    list held. Ref *order* may differ from the post-merge list, but the graph
    fingerprint is order-independent (edge set + coverage counts), so output is
    unchanged."""

    def __init__(self, checkpoint_root: str, repo_tag: str, num_batches: int, total: int | None = None):
        self._root = checkpoint_root
        self._repo = repo_tag
        self._num_batches = num_batches
        self._len = total

    def __iter__(self):
        # One-batch-ahead prefetch: loading + deserializing a batch file is
        # disk/CPU work that would otherwise serialize with the caller's
        # per-ref loop. A single daemon thread keeps exactly one next batch in
        # flight (maxsize=1), so iteration ORDER is unchanged and at most two
        # batches are resident at once (the one being consumed + the prefetched
        # one). The stop event lets an abandoned iteration (cancelled resolve)
        # end the loader promptly instead of leaking a thread that pins a
        # loaded batch for the life of the worker process.
        stop = threading.Event()
        done = object()
        q: Queue = Queue(maxsize=1)

        def _put(item) -> bool:
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.5)
                    return True
                except Full:
                    continue
            return False

        def _loader() -> None:
            for i in range(self._num_batches):
                if stop.is_set():
                    return
                if not _put(load_batch(self._root, self._repo, i)):
                    return
            _put(done)

        loader = threading.Thread(target=_loader, name="refstream-prefetch", daemon=True)
        loader.start()
        try:
            while True:
                batch = q.get()
                if batch is done:
                    break
                if batch is not None:
                    yield from batch.refs
        finally:
            stop.set()

    def __len__(self) -> int:
        if self._len is None:
            self._len = sum(1 for _ in self)
        return self._len


_RESOLVE_STATE_NAME = "resolve_state.joblib"


def save_resolve_checkpoint(checkpoint_root: str, repo_tag: str, state: dict) -> None:
    """Persist resolve()'s in-progress state (next ref index, accumulated
    output, dedup maps, coverage) so a crash mid-resolve can resume instead of
    reprocessing every ref from scratch. `state["total_refs"]` is stored for
    shape validation on load — same pattern as the extraction manifest.

    Lives in the same per-repo checkpoint directory as the extraction batch
    files, so the single `clear_checkpoint()` call after a fully successful
    run cleans up both.
    """
    d = checkpoint_dir_for(checkpoint_root, repo_tag)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _RESOLVE_STATE_NAME)
    tmp_path = path + ".tmp"
    joblib.dump(state, tmp_path)
    os.replace(tmp_path, path)  # atomic — a crash mid-write never leaves a partial state file


def load_resolve_checkpoint(checkpoint_root: str, repo_tag: str, total_refs: int) -> dict | None:
    """Return the saved resolve state if present and matching this run's ref
    count, else None (discard silently — resolve() will start from scratch)."""
    path = os.path.join(checkpoint_dir_for(checkpoint_root, repo_tag), _RESOLVE_STATE_NAME)
    try:
        state = joblib.load(path)
    except (OSError, EOFError, ValueError) as exc:
        _log.warning("[graph_checkpoint][repo=%s] failed to load resolve state: %s", repo_tag, exc)
        return None
    if not isinstance(state, dict) or state.get("total_refs") != total_refs:
        _log.warning(
            "[graph_checkpoint][repo=%s] discarding stale resolve checkpoint (shape changed)", repo_tag,
        )
        return None
    return state

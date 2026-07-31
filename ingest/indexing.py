"""Incremental graph-build core — copied + trimmed from developer_assistant's
graph_review_runner.py (ensure_graph_indexed / GraphIndexLockedError /
slugify_project only). Confirmed pure graph-build with zero analyzer/LLM
coupling: the only imports this function actually needs are graph_core's own
config/discovery/pipeline/store modules plus stdlib — the original file's
sail_core.exceptions.database_exceptions.DatabaseConnectionError,
app.core.config.settings, and analyzer/graph_llm_adapter/two_agent_runner
imports are all used only by that file's OTHER functions (run_graph_review
and friends), never by ensure_graph_indexed() itself, so none of them are
carried over here.

Logic is otherwise unchanged from the original.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Callable, List, Optional

from graph_core.config import get_lock_stale_seconds
from graph_core.discovery import build_manifest, codebase_hash
from graph_core.pipeline import IndexResult, index_repo
from graph_core.store import GraphStore
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


class GraphIndexLockedError(RuntimeError):
    """Raised when another build already holds the per-namespace index lock."""


def slugify_project(name: str) -> str:
    """Turn a user-supplied project name into a stable namespace segment.

    Lowercases, keeps alnum/dash/underscore, collapses everything else to a
    single '-'. Deliberately simple/deterministic — the same project name
    must always slug to the same namespace so a re-ingest maps back to the
    same persistent graph."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "project"


def ensure_graph_indexed(
    src_path: str,
    repo_tag: str,
    store: GraphStore,
    *,
    javac: bool = False,
    bytecode: bool = False,
    on_stage: Optional[Callable[[str, dict], None]] = None,
    candidate_relpaths: Optional[List[str]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    job_id: Optional[str] = None,
) -> Optional[IndexResult]:
    """Bring the persistent Neo4j graph for ``repo_tag`` up to date, doing the
    minimum work necessary.

    Hashes the current codebase and compares it against the namespace's
    ``GraphMeta`` from the last successful ingest:
      * hash unchanged -> returns ``None`` immediately, no ingest at all.
      * hash changed / first ingest -> re-extracts only the changed files
        (unchanged files load their bundle from the extraction cache),
        re-resolves globally, patches Neo4j, and returns the ``IndexResult``.

    Holds the per-namespace lock (``GraphStore.acquire_lock``/``release_lock``)
    for the duration of the check + any ingest it performs. If another build
    of the same namespace is already indexing, raises ``GraphIndexLockedError``
    immediately (fail-fast) rather than waiting.
    """
    logfn = log or (lambda m: _log.info("%s", m))
    token = uuid.uuid4().hex
    stale_before = time.time() - get_lock_stale_seconds()
    if not store.acquire_lock(repo_tag, token, stale_before, job_id=job_id):
        raise GraphIndexLockedError(
            f"a graph build for '{repo_tag}' is already in progress; try again shortly"
        )
    has_baseline = False
    try:
        prev_meta = store.get_graph_meta(repo_tag) or {}
        prev_hash = prev_meta.get("codebase_hash")
        # Only a previously *successful* ingest is a safe diff baseline — a
        # namespace left in "indexing"/"error" status (crash, or another
        # process's write in flight) may have partial/inconsistent nodes.
        has_baseline = bool(prev_hash) and prev_meta.get("status") == "ready"

        current_manifest = build_manifest(src_path, candidate_relpaths=candidate_relpaths)
        current_hash = codebase_hash(current_manifest)

        if has_baseline and prev_hash == current_hash:
            logfn(f"[graph_build] {repo_tag} unchanged since last ingest — skipping re-index")
            return None

        changed_files: Optional[List[str]] = None
        deleted_files: Optional[List[str]] = None
        if has_baseline:
            prev_shas = store.file_shas(repo_tag)
            changed_files = sorted(
                relpath for relpath, sha in current_manifest.items()
                if prev_shas.get(relpath) != sha
            )
            deleted_files = sorted(set(prev_shas.keys()) - set(current_manifest.keys()))
            logfn(
                f"[graph_build] {repo_tag} changed: {len(changed_files)} file(s), "
                f"deleted: {len(deleted_files)} file(s) (of {len(current_manifest)} total)"
            )
        else:
            logfn(f"[graph_build] {repo_tag} first ingest ({len(current_manifest)} file(s))")

        store.upsert_graph_meta(repo_tag, {"status": "indexing"})
        index_result = index_repo(
            src_path,
            repo_tag,
            store,
            wipe=not has_baseline,
            javac=javac,
            bytecode=bytecode,
            on_stage=on_stage,
            candidate_files=candidate_relpaths,
            cancel_check=cancel_check,
            changed_files=changed_files,
            deleted_files=deleted_files,
        )
        node_count, edge_count = store.counts(repo_tag)
        store.upsert_graph_meta(repo_tag, {
            "codebase_hash": current_hash,
            "file_count": len(current_manifest),
            "node_count": node_count,
            "edge_count": edge_count,
            "last_indexed_at": time.time(),
            "status": "ready",
        })
        return index_result
    except Exception:
        # Mark the namespace "error" so a subsequent call never mistakes a
        # half-written graph for a valid up-to-date one; it will simply
        # re-ingest next time.
        #
        # The partial graph is deliberately KEPT rather than wiped, so a failed
        # build can be inspected instead of vanishing — which is the whole point
        # of an experimentation harness. This is safe without a wipe because the
        # status flag alone already fences it off:
        #   * has_baseline (above) requires status == "ready", so a partial
        #     graph is never used as an incremental diff baseline;
        #   * the next attempt therefore computes wipe=not has_baseline -> True,
        #     and index_repo clears the namespace before writing anything.
        # So the partial nodes are cleaned up by the next run regardless; wiping
        # here only destroyed the evidence early.
        try:
            store.upsert_graph_meta(repo_tag, {"status": "error"})
        except Exception:  # noqa: BLE001
            pass
        if not has_baseline:
            logfn(
                f"[graph_build] {repo_tag} first-ingest attempt failed/cancelled — "
                f"partial graph left in place for inspection (status=error; "
                f"the next build wipes and re-ingests it)"
            )
        raise
    finally:
        store.release_lock(repo_tag, token)

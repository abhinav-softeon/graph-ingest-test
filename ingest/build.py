"""Thin orchestration: zip -> temp source dir -> ensure_graph_indexed() ->
memory/time instrumentation -> cleanup. NEW glue code (not copied from
developer_assistant) tying the copied graph_core + ensure_graph_indexed
together with the instrumentation this app exists for.
"""
from __future__ import annotations

import shutil
import uuid
from typing import Any, Callable, Optional

from graph_core.config import (
    is_bytecode_resolver_enabled,
    checkpoint_root,
    extract_batch_size,
    extract_worker_count,
    get_cache_io_workers,
    get_extract_cache_dir,
    get_zip_extract_workers,
    is_extract_cache_enabled,
    is_javac_resolver_enabled,
    is_streaming_ingest_enabled,
    neo4j_config,
    javac_timeout_seconds,
)
from graph_core.store import GraphStore
from instrumentation.memory_sampler import MemorySampler
from instrumentation.run_report import RunReport
from sail_core.logger.logger import get_logger

from .indexing import ensure_graph_indexed, slugify_project
from .upload_utils import SUPPORTED_CODE_EXTS, materialize_uploaded_sources_from_zip_path

_log = get_logger(__name__)


def _config_snapshot(project: str, repo_tag: str, javac: Optional[bool],
                     bytecode: Optional[bool] = None) -> dict[str, Any]:
    """Read every toggle graph_core/config.py currently exposes, fresh, so
    the run report accurately records what was actually active for this run
    (env vars may have been changed by the caller right before this call)."""
    return {
        "project": project,
        "repo_tag": repo_tag,
        "javac": javac if javac is not None else is_javac_resolver_enabled(),
        "bytecode": bytecode if bytecode is not None else is_bytecode_resolver_enabled(),
        "javac_timeout_s": javac_timeout_seconds(),
        "extract_batch_size": extract_batch_size(),
        "extract_workers": extract_worker_count(),
        "checkpoint_root": checkpoint_root(),
        "streaming_ingest": is_streaming_ingest_enabled(),
        "extract_cache_enabled": is_extract_cache_enabled(),
        "extract_cache_dir": get_extract_cache_dir(),
        "cache_io_workers": get_cache_io_workers(),
        "zip_extract_workers": get_zip_extract_workers(),
    }


def build_graph_from_zip(
    zip_path: str,
    project: str,
    *,
    javac: Optional[bool] = None,
    bytecode: Optional[bool] = None,
    on_stage: Optional[Callable[[str, dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    sample_interval_s: float = 0.5,
    runs_dir: str = "runs",
) -> dict:
    """Extract ``zip_path``, ingest it into Neo4j under ``project``'s
    namespace, and return the finished run report (dict) — see
    instrumentation/run_report.py for its shape. Raises
    ``ingest.indexing.GraphIndexLockedError`` if another build of the same
    project is already in progress, and re-raises any ingest failure after
    recording it in the report.
    """
    run_id = uuid.uuid4().hex[:8]
    repo_tag = slugify_project(project)
    sampler = MemorySampler(interval_s=sample_interval_s)
    config_snapshot = _config_snapshot(project, repo_tag, javac, bytecode)
    report = RunReport(run_id, project, config_snapshot, sampler, runs_dir=runs_dir, inner_on_stage=on_stage)
    sampler.start()

    _log.info("[build][run=%s] extracting zip for project=%s", run_id, project)
    tmp_root, selected = materialize_uploaded_sources_from_zip_path(
        zip_path,
        allowed_exts=SUPPORTED_CODE_EXTS,
        on_progress=lambda done, total, name: report.on_stage(
            "unzipping", {"done": done, "total": total, "file": name}
        ),
    )
    store: Optional[GraphStore] = None
    try:
        store = GraphStore(neo4j_config())
        store.bootstrap()
        index_result = ensure_graph_indexed(
            tmp_root,
            repo_tag,
            store,
            javac=config_snapshot["javac"],
            bytecode=config_snapshot["bytecode"],
            on_stage=report.on_stage,
            candidate_relpaths=selected,
            cancel_check=cancel_check,
        )
        if index_result is not None:
            report.set_result_counts(
                files=index_result.files,
                nodes=index_result.nodes,
                edges=index_result.edges,
                stage_seconds=index_result.stage_seconds,
                validation=index_result.validation,
                # Which precise tier actually covered what. Without these the
                # report cannot distinguish "bytecode resolved everything" from
                # "bytecode silently fell back to the heuristic" — the two look
                # identical in node/edge counts.
                bytecode=index_result.bytecode.summary(),
                javac={
                    "available": index_result.javac.available,
                    "reason": index_result.javac.reason,
                    "edges": index_result.javac.edges,
                    "file_coverage": round(index_result.javac.file_coverage, 4),
                    "attribution_rate": round(index_result.javac.attribution_rate, 4),
                    "attributed_file_count": index_result.javac.attributed_file_count,
                    "seconds": round(index_result.javac.seconds, 2),
                },
            )
        else:
            report.set_result_counts(skipped_unchanged=True)
        return report.finish()
    except Exception as exc:  # noqa: BLE001 - recorded in the report, then re-raised
        report.finish(error=str(exc))
        raise
    finally:
        sampler.stop()
        if store is not None:
            store.close()
        shutil.rmtree(tmp_root, ignore_errors=True)

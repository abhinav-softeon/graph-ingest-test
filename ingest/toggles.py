"""Translates this app's checkboxes/settings into the GRAPH_* env vars that
graph_core/config.py reads fresh on every call — the single place all three
entrypoints (cli.py, api.py, ui/app.py) share identical checkbox -> env-var
semantics, so a setting means the same thing regardless of which entrypoint
is used.
"""
from __future__ import annotations

import os
from typing import Optional

# GRAPH_EXTRACT_BATCH_SIZE set this large effectively means "one giant batch"
# (batch_starts = list(range(0, total_files, batch_size)) yields exactly one
# entry for any realistic file count) — i.e. "raw / no chunking" mode, without
# needing to know the true file count in advance.
_RAW_BATCH_SIZE_SENTINEL = 1_000_000_000


def apply_ingestion_toggles(
    *,
    chunking: bool = True,
    extract_batch_size: Optional[int] = None,
    parallel_extraction: bool = True,
    extract_workers: Optional[int] = None,
    checkpointing: bool = False,
    checkpoint_root: Optional[str] = None,
    streaming_ingest: bool = False,
    streaming_writer: bool = False,
    scip: bool = False,
    extract_cache: bool = True,
    extract_cache_dir: Optional[str] = None,
    cache_io_workers: Optional[int] = None,
    zip_extract_workers: Optional[int] = None,
    resolve_workers: Optional[int] = None,
    write_workers: Optional[int] = None,
) -> None:
    """Set every GRAPH_* env var this app exposes from plain Python values.

    Streaming ingest/writer are nested under checkpointing on purpose:
    pipeline.py's streaming paths require disk-spill (``spilling`` = a
    checkpoint root being set) before they can activate at all, so passing
    streaming_ingest=True with checkpointing=False here is a no-op rather
    than a silent contradiction.
    """
    os.environ["GRAPH_EXTRACT_BATCH_SIZE"] = str(extract_batch_size or 2000) if chunking else str(_RAW_BATCH_SIZE_SENTINEL)

    os.environ["GRAPH_EXTRACT_WORKERS"] = "1" if not parallel_extraction else str(extract_workers or (os.cpu_count() or 4))

    if checkpointing:
        os.environ["GRAPH_CHECKPOINT_ROOT"] = checkpoint_root or os.path.join(".graph_checkpoints")
    else:
        os.environ["GRAPH_CHECKPOINT_ROOT"] = ""

    os.environ["GRAPH_STREAMING_INGEST"] = "true" if (checkpointing and streaming_ingest) else "false"
    os.environ["GRAPH_STREAMING_WRITER"] = "true" if (checkpointing and streaming_ingest and streaming_writer) else "false"

    os.environ["GRAPH_SCIP_ENABLED"] = "true" if scip else "false"

    os.environ["GRAPH_EXTRACT_CACHE_ENABLED"] = "true" if extract_cache else "false"
    if extract_cache_dir:
        os.environ["GRAPH_EXTRACT_CACHE_DIR"] = extract_cache_dir

    if cache_io_workers:
        os.environ["GRAPH_CACHE_IO_WORKERS"] = str(cache_io_workers)
    if zip_extract_workers:
        os.environ["GRAPH_ZIP_EXTRACT_WORKERS"] = str(zip_extract_workers)

    os.environ["GRAPH_RESOLVE_WORKERS"] = str(resolve_workers or 1)

    os.environ["GRAPH_WRITE_WORKERS"] = str(write_workers or 1)

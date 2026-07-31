"""FastAPI entrypoint: POST /build (multipart zip + form toggles) runs the
ingest in a background task; GET /build/{id} polls status + the final run
report. In-memory, single-process — this is an experimentation app, not
developer_assistant's durable DB/SSE job system.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from ingest.build import build_graph_from_zip
from ingest.toggles import apply_ingestion_toggles

app = FastAPI(title="graph_build_test")

_BUILDS: dict[str, dict] = {}


def _run_build(build_id: str, zip_path: str, project: str, toggles: dict) -> None:
    _BUILDS[build_id]["status"] = "running"
    try:
        apply_ingestion_toggles(**toggles)
        report = build_graph_from_zip(zip_path, project, runs_dir="runs")
        _BUILDS[build_id]["status"] = "done"
        _BUILDS[build_id]["report"] = report
    except Exception as exc:  # noqa: BLE001 - surfaced via the status endpoint
        _BUILDS[build_id]["status"] = "error"
        _BUILDS[build_id]["error"] = str(exc)
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass


@app.post("/build")
async def start_build(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    project: str = Form(...),
    chunking: bool = Form(True),
    extract_batch_size: Optional[int] = Form(None),
    parallel_extraction: bool = Form(True),
    extract_workers: Optional[int] = Form(None),
    checkpointing: bool = Form(False),
    checkpoint_root: Optional[str] = Form(None),
    streaming_ingest: bool = Form(False),
    javac: bool = Form(False),
    bytecode: bool = Form(False),
    extract_cache: bool = Form(True),
    extract_cache_dir: Optional[str] = Form(None),
    cache_io_workers: Optional[int] = Form(None),
    zip_extract_workers: Optional[int] = Form(None),
    write_workers: int = Form(1),
):
    build_id = uuid.uuid4().hex[:12]
    fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip", prefix=f"upload_{build_id}_")
    with os.fdopen(fd, "wb") as fh:
        fh.write(await file.read())

    toggles = dict(
        chunking=chunking,
        extract_batch_size=extract_batch_size,
        parallel_extraction=parallel_extraction,
        extract_workers=extract_workers,
        checkpointing=checkpointing,
        checkpoint_root=checkpoint_root,
        streaming_ingest=streaming_ingest,
        javac=javac,
        bytecode=bytecode,
        extract_cache=extract_cache,
        extract_cache_dir=extract_cache_dir,
        cache_io_workers=cache_io_workers,
        zip_extract_workers=zip_extract_workers,
        write_workers=write_workers,
    )
    _BUILDS[build_id] = {"status": "queued", "project": project}
    background_tasks.add_task(_run_build, build_id, tmp_zip_path, project, toggles)
    return {"build_id": build_id}


@app.get("/build/{build_id}")
async def get_build(build_id: str):
    build = _BUILDS.get(build_id)
    if build is None:
        return JSONResponse({"error": "unknown build_id"}, status_code=404)
    return build

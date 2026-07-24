"""Streamlit UI — Graph Build Experimentation Harness.

Every ingestion-mode/concurrency setting is a checkbox or number input;
each run's timing+memory report is shown as soon as it finishes. Modeled on
playwrightautomation/app.py's conventions (_env/_env_bool helpers,
load_dotenv, sidebar controls) since that's this workspace's existing
Streamlit pattern.

Run from the graph_build_test/ directory:
    streamlit run ui/app.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import streamlit as st

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=False)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


st.set_page_config(page_title="Graph Build Experimentation", page_icon="\U0001F578", layout="wide")

st.title("Graph Build Experimentation Harness")
st.caption(
    "Ingest a zip into Neo4j with every memory/time-affecting setting exposed \u2014 "
    "for comparing raw vs. chunked vs. parallel vs. checkpointed runs."
)

with st.sidebar:
    st.header("Neo4j")
    neo4j_uri = st.text_input("URI", value=_env("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user = st.text_input("User", value=_env("NEO4J_USER", "neo4j"))
    neo4j_password = st.text_input("Password", value=_env("NEO4J_PASSWORD", "testpassword"), type="password")
    neo4j_database = st.text_input("Database", value=_env("NEO4J_DATABASE", "neo4j"))

    st.divider()
    st.header("Ingestion mode")
    chunking = st.checkbox("Chunked discover+extract", value=True, help='Off = one giant batch ("raw", closest to pre-optimization behavior)')
    extract_batch_size = st.number_input("Batch size", min_value=1, value=2000, disabled=not chunking)

    parallel_extraction = st.checkbox("Parallel extraction", value=True, help="Off = force single-process sequential extraction")
    extract_workers = st.number_input("Extraction workers", min_value=1, value=(os.cpu_count() or 4), disabled=not parallel_extraction)

    checkpointing = st.checkbox("Disk-spill checkpointing", value=False)
    checkpoint_root = st.text_input("Checkpoint dir", value="./.graph_checkpoints", disabled=not checkpointing)
    streaming_ingest = st.checkbox("Streaming ingest (needs checkpointing)", value=False, disabled=not checkpointing)
    streaming_writer = st.checkbox("Streaming writer (needs streaming ingest)", value=False, disabled=not (checkpointing and streaming_ingest))

    st.divider()
    st.header("Optional subsystems")
    scip = st.checkbox("SCIP precise resolution", value=False)
    extract_cache = st.checkbox("Local extract cache", value=True)
    extract_cache_dir = st.text_input("Extract cache dir", value="./.cache/graph_extract_cache", disabled=not extract_cache)

    st.divider()
    st.header("Concurrency (advanced)")
    cache_io_workers = st.number_input("Extract-cache I/O threads", min_value=1, value=16)
    zip_extract_workers = st.number_input("Zip-extraction threads", min_value=1, value=min(16, max(1, (os.cpu_count() or 4) * 2)))
    resolve_workers = st.number_input(
        "Resolve workers (EXPERIMENTAL)", min_value=1, value=1,
        help="1 = sequential resolve() (default, proven). >1 = new parallel path \u2014 "
             "may use MORE memory on Windows (spawn, not fork). Measure, don't assume.",
    )

    st.divider()
    sample_interval = st.number_input("Memory sample interval (s)", min_value=0.1, value=0.5, step=0.1)

st.header("Run")
zip_source = st.radio("Zip source", ["Upload", "Local path (recommended for huge codebases)"], horizontal=True)
if zip_source == "Upload":
    uploaded = st.file_uploader("Zip of source code", type=["zip"])
    zip_path_input = None
else:
    uploaded = None
    zip_path_input = st.text_input("Local .zip path")

project = st.text_input("Project name", value="experiment")
run_btn = st.button("Build graph", type="primary")

status_box = st.empty()

if run_btn:
    if uploaded is None and not zip_path_input:
        st.error("Provide a zip (upload or local path).")
    else:
        os.environ["NEO4J_URI"] = neo4j_uri
        os.environ["NEO4J_USER"] = neo4j_user
        os.environ["NEO4J_PASSWORD"] = neo4j_password
        os.environ["NEO4J_DATABASE"] = neo4j_database

        from ingest.build import build_graph_from_zip
        from ingest.toggles import apply_ingestion_toggles

        apply_ingestion_toggles(
            chunking=chunking,
            extract_batch_size=int(extract_batch_size),
            parallel_extraction=parallel_extraction,
            extract_workers=int(extract_workers),
            checkpointing=checkpointing,
            checkpoint_root=checkpoint_root,
            streaming_ingest=streaming_ingest,
            streaming_writer=streaming_writer,
            scip=scip,
            extract_cache=extract_cache,
            extract_cache_dir=extract_cache_dir,
            cache_io_workers=int(cache_io_workers),
            zip_extract_workers=int(zip_extract_workers),
            resolve_workers=int(resolve_workers),
        )

        if uploaded is not None:
            tmp_dir = tempfile.mkdtemp(prefix="streamlit_upload_")
            tmp_zip_path = os.path.join(tmp_dir, uploaded.name)
            with open(tmp_zip_path, "wb") as fh:
                fh.write(uploaded.getbuffer())
        else:
            tmp_zip_path = zip_path_input

        def _on_stage(stage, detail):
            status_box.info(f"Stage: **{stage}** \u2014 {detail}")

        with st.spinner("Building graph..."):
            try:
                report = build_graph_from_zip(
                    tmp_zip_path,
                    project,
                    on_stage=_on_stage,
                    sample_interval_s=sample_interval,
                    runs_dir="runs",
                )
                status_box.success(f"Done in {report['total_duration_s']}s \u2014 peak {report['overall_peak_mb']} MB")
                st.subheader("Stages")
                st.dataframe(report["stages"])
                st.subheader("Full report")
                st.json(report)
            except Exception as exc:
                status_box.error(f"Failed: {exc}")

st.divider()
st.caption("Past runs: see runs/index.jsonl for a summary of every run, runs/<id>.json for full detail.")

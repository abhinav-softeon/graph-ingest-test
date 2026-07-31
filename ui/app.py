"""Streamlit UI — Graph Build Experimentation Harness.

Ingest a zip into Neo4j with every memory/time-affecting setting exposed. A big
live stopwatch + live RAM readout tick during the run.

    NOTE: the joblib "dump → load" workflow is currently DISABLED (kept as a
    possibility — see pipeline.py / cli.py / load_graph_to_neo4j.py). This UI
    does the full graph build straight into Neo4j.

Run from the graph_build_test/ directory:
    streamlit run ui/app.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import psutil
import streamlit as st

# Quiet Streamlit's own INFO/WARNING chatter (chiefly the repeated "missing
# ScriptRunContext" line) so it doesn't drown out graph_ingest's stage/memory
# logs. Errors still surface, and non-Streamlit logging is untouched.
#
# This MUST go through streamlit.logger.set_log_level, not
# logging.getLogger(...).setLevel(): streamlit/logger.py's get_logger() resets
# every logger it creates to its own global level, and the modules that emit
# this warning are imported lazily on the first st.* call — i.e. AFTER any
# setLevel() here — so a plain setLevel is silently undone. set_log_level
# updates the global default as well as the already-created loggers.
try:
    from streamlit.logger import set_log_level as _st_set_log_level
    _st_set_log_level("error")
except Exception:  # noqa: BLE001 - private API; never break the app over logging
    logging.getLogger("streamlit").setLevel(logging.ERROR)

# Running this file with plain `python ui/app.py` starts Streamlit in "bare
# mode": every widget call is a no-op, so NOTHING renders and you get one
# "missing ScriptRunContext" warning per widget. Fail loudly instead of leaving
# someone staring at an empty terminal wondering where the UI went.
if not st.runtime.exists():
    sys.exit(
        "\nThis is a Streamlit app — running it with `python` renders nothing.\n"
        "Start it with:\n\n    streamlit run ui/app.py\n"
    )

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), override=False)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _fmt_elapsed(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _current_rss_mb() -> float:
    """Whole-process RSS + live child processes (parallel extraction workers)."""
    proc = psutil.Process()
    total = proc.memory_info().rss
    try:
        for c in proc.children(recursive=True):
            try:
                total += c.memory_info().rss
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    return total / (1024 * 1024)


def run_with_stopwatch(work, stage_state: dict, status_box) -> dict:
    """Run ``work()`` on a worker thread while a big stopwatch + live RAM readout
    tick on the main thread. Returns a dict with ``value`` (work's return) or
    ``error``.

    st.* is only ever touched here (the main thread). ``work()`` and its
    callbacks must only mutate ``stage_state['text']`` — never call st.*.
    """
    result: dict = {}

    def _worker():
        try:
            result["value"] = work()
        except Exception as exc:  # surfaced to the caller
            result["error"] = exc

    timer_box = st.empty()
    proc = psutil.Process()
    peak_mb = 0.0

    def _rss_mb() -> float:
        # Whole-process RSS + live child processes (parallel extraction spawns
        # workers) — mirrors instrumentation/memory_sampler.py so the live number
        # matches the run report's peak.
        total = proc.memory_info().rss
        try:
            for c in proc.children(recursive=True):
                try:
                    total += c.memory_info().rss
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        return total / (1024 * 1024)

    def _render(secs: float, done: bool) -> None:
        nonlocal peak_mb
        rss_mb = _rss_mb()
        peak_mb = max(peak_mb, rss_mb)
        color = "#16a34a" if done else "#2563eb"
        timer_box.markdown(
            f"<div style='font-size:5rem;font-weight:700;line-height:1;"
            f"font-variant-numeric:tabular-nums;color:{color}'>"
            f"⏱️ {_fmt_elapsed(secs)}</div>"
            f"<div style='font-size:1.15rem;color:#64748b;font-variant-numeric:tabular-nums'>"
            f"RAM {rss_mb:,.0f} MB&nbsp;·&nbsp;peak {peak_mb:,.0f} MB</div>",
            unsafe_allow_html=True,
        )

    worker = threading.Thread(target=_worker, name="graph-build", daemon=True)
    start = time.time()
    worker.start()
    while worker.is_alive():
        _render(time.time() - start, done=False)
        status_box.info(f"Stage: {stage_state['text']}")
        time.sleep(0.25)
    worker.join()
    _render(time.time() - start, done=True)
    return result


def _apply_neo4j_env(uri: str, user: str, password: str, database: str) -> None:
    os.environ["NEO4J_URI"] = uri
    os.environ["NEO4J_USER"] = user
    os.environ["NEO4J_PASSWORD"] = password
    os.environ["NEO4J_DATABASE"] = database


st.set_page_config(page_title="Graph Build Experimentation", page_icon="\U0001F578", layout="wide")

st.title("Graph Build Experimentation Harness")
st.caption(
    "Ingest a zip into Neo4j with every memory/time-affecting setting exposed — "
    "for comparing raw vs. chunked vs. parallel vs. checkpointed runs."
)

with st.sidebar:
    st.header("Neo4j")
    neo4j_uri = st.text_input("URI", value=_env("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user = st.text_input("User", value=_env("NEO4J_USER", "neo4j"))
    neo4j_password = st.text_input("Password", value=_env("NEO4J_PASSWORD", "testpassword"), type="password")
    neo4j_database = st.text_input("Database", value=_env("NEO4J_DATABASE", "neo4j"))

    st.divider()
    st.header("Profile")
    # The remaining memory levers interact, and the right combination is not
    # obvious from the individual knobs: ref streaming does nothing without
    # checkpointing, and extraction RAM is workers x batch size, not either
    # alone. Everything else that used to matter is unconditional now
    # (MEMORY_ARCHITECTURE_PLAN.md items #1/#2/#2b/#14), so these are the only
    # dials left. Picking a profile sets them; each is still editable after.
    _PROFILES = {
        "Balanced": dict(chunk=True, batch=2000, par=True, workers=(os.cpu_count() or 4),
                         ckpt=False, stream=False, wworkers=1, wbatch=5000),
        "Low RAM":  dict(chunk=True, batch=500,  par=True, workers=2,
                         ckpt=True,  stream=True,  wworkers=1, wbatch=2000),
        "Fast":     dict(chunk=True, batch=4000, par=True, workers=(os.cpu_count() or 4),
                         ckpt=False, stream=False, wworkers=4, wbatch=10000),
    }
    profile = st.radio(
        "Preset", list(_PROFILES), horizontal=True,
        help="Low RAM: small batches, few extraction workers, refs streamed from disk "
             "(needs checkpointing), sequential small writes. Fast: bigger batches and "
             "concurrent writes, more RAM and more Neo4j transaction memory. "
             "Changing the preset resets the dials below.",
    )
    _P = _PROFILES[profile]
    _k = profile.replace(" ", "_")  # re-key on change so the widgets actually reset

    st.divider()
    st.header("Ingestion mode")
    chunking = st.checkbox("Chunked discover+extract", value=_P["chunk"], key=f"chunk_{_k}",
                           help='Off = one giant batch ("raw", closest to pre-optimization behavior)')
    extract_batch_size = st.number_input("Extract batch size (files)", min_value=1, value=_P["batch"],
                                         disabled=not chunking, key=f"batch_{_k}",
                                         help="Files read+parsed per batch. Raw file content is the "
                                              "extraction peak, and it is bounded by this x workers.")

    parallel_extraction = st.checkbox("Parallel extraction", value=_P["par"], key=f"par_{_k}",
                                      help="Off = force single-process sequential extraction")
    extract_workers = st.number_input("Extraction workers", min_value=1, value=_P["workers"],
                                      disabled=not parallel_extraction, key=f"workers_{_k}",
                                      help="Each worker holds its own batch, so extraction RAM is "
                                           "roughly workers x batch size. Windows uses spawn, not "
                                           "fork, so workers do not share memory copy-on-write.")

    checkpointing = st.checkbox("Disk-spill checkpointing", value=_P["ckpt"], key=f"ckpt_{_k}",
                                help="Spills each extraction batch to disk and drops it from RAM, "
                                     "instead of accumulating every batch until resolve. Also the "
                                     "prerequisite for ref streaming below, and enables crash-resume.")
    checkpoint_root = st.text_input("Checkpoint dir", value="./.graph_checkpoints", disabled=not checkpointing)
    streaming_ingest = st.checkbox(
        "Stream refs from disk (needs checkpointing)", value=_P["stream"] and _P["ckpt"],
        disabled=not checkpointing, key=f"stream_{_k}",
        help="Streams the ref list from disk during resolve instead of holding it in RAM. "
             "At ~150 bytes per ref and millions of refs on a large repo, that list is now "
             "one of the largest things left — it scales with call sites rather than "
             "declarations. Everything else this used to gate is unconditional: node/edge "
             "early-write to Neo4j (items #1/#2), the slim node projection that resolve reads "
             "(item #14), and dropping the bulk edges after their write (item #2b).",
    )

    st.divider()
    st.header("Optional subsystems")
    bytecode = st.checkbox(
        "bytecode resolver (.class / .jar)", value=False,
        help="Read Java calls straight out of compiled bytecode. Every call "
             "instruction already names its owner class, method and exact "
             "signature — javac RE-DERIVES that; a class file simply carries "
             "it. Nothing is inferred, so nothing can be mis-inferred.\n\n"
             "It is also the only source for three things no source-level pass "
             "can give you: lambda bodies, anonymous inner classes and static "
             "initializers as real nodes (tree-sitter has no declaration to "
             "find), and field READS with the exact owning class rather than "
             "only explicit `this.x`.\n\n"
             "Needs compiled output in the upload (target/classes, "
             "WEB-INF/classes, or jars). Coverage is per-file: anything without "
             "a class file falls through to javac or the heuristic. If the "
             "class files turn out to be STALE relative to the source, the "
             "whole pass is discarded rather than trusted.",
    )
    bytecode_roots = st.text_input(
        "bytecode class roots (optional)", value="", disabled=not bytecode,
        placeholder=r"D:\path\to\classes",
        help="Leave empty to auto-discover .class directories and jars inside "
             "the uploaded zip.\n\n"
             "Set this when the compiled output lives OUTSIDE the upload — which "
             "is the normal case if you built the repo yourself with "
             "scripts/compile_repo.py. Separate multiple locations with ';' on "
             "Windows (':' elsewhere). Directories are walked for .class files; "
             ".jar/.war/.ear are read directly.",
    )
    javac = st.checkbox(
        "javac resolver for Java CALLS", value=False,
        help="Resolve Java calls with the real compiler instead of heuristic "
             "name matching. Measured against javac ground truth on a 16.5k-file "
             "Java repo, the heuristic scored 93.8% recall but 5.0% PRECISION — it "
             "finds the right target and then emits every same-named candidate "
             "beside it. javac has the type system.\n\n"
             "Needs a JDK (javac + java on PATH) but NOT a build system: it "
             "resolves in-repo types from -sourcepath alone, so no Maven/Gradle "
             "is required. Runs BEFORE resolve, so it replaces that work rather "
             "than adding to it. Falls back to the heuristic on any failure.\n\n"
             "Java CALLS only — every other edge type and language is unaffected.",
    )
    javac_timeout = st.number_input(
        "javac timeout (seconds)", min_value=60, max_value=86_400, value=3600, step=300,
        disabled=not javac,
        help="Wall-clock budget for the attribution pass. Pure type resolution — "
             "no dependency download, no artifact build — but a large monorepo "
             "still takes real time. On timeout Java falls back to the heuristic.",
    )
    javac_batch = st.number_input(
        "javac batch size (files)", min_value=25, max_value=5_000, value=400, step=25,
        disabled=not javac,
        help="Files per javac task. Attribution holds the whole batch's symbol "
             "table, so this is the MEMORY knob — if javac runs out of heap, "
             "lower this rather than raising -Xmx.",
    )
    extract_cache = st.checkbox("Local extract cache", value=True)
    extract_cache_dir = st.text_input("Extract cache dir", value="./.cache/graph_extract_cache", disabled=not extract_cache)

    from graph_core.config import compiled_hotpath_status
    _hotpath = compiled_hotpath_status()
    st.checkbox(
        "Compiled hot-path active (resolver.py)", value=_hotpath["resolver"], disabled=True,
        help="Read-only status, not a switch: whether resolver.py loaded as a compiled "
             "Cython extension (faster resolve()) or plain interpreted Python. Built via "
             "setup_cython.py — see its docstring. Same algorithm/memory either way.",
    )
    st.checkbox(
        "Compiled hot-path active (pipeline.py)", value=_hotpath["pipeline"], disabled=True,
        help="Read-only status for pipeline.py (the derive passes) — see above.",
    )

    st.divider()
    st.header("Concurrency (advanced)")
    cache_io_workers = st.number_input("Extract-cache I/O threads", min_value=1, value=16)
    zip_extract_workers = st.number_input("Zip-extraction threads", min_value=1, value=min(16, max(1, (os.cpu_count() or 4) * 2)))
    write_workers = st.number_input(
        "Neo4j write workers", min_value=1, value=_P["wworkers"], key=f"wworkers_{_k}",
        help="1 = sequential batch writes (default). >1 = concurrent write batches over "
             "threads (I/O-bound, no fork/spawn cost) using retry-safe managed transactions.",
    )
    write_batch_size = st.number_input(
        "Neo4j write batch size (rows/txn)", min_value=1, value=_P["wbatch"], step=1000,
        key=f"wbatch_{_k}",
        help="Rows per write transaction. Smaller = less transaction memory (safer against "
             "dbms.memory.transaction.total.max, especially with >1 write worker) but more "
             "commits; larger = fewer commits but heavier transactions.",
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
_col_run, _col_unlock = st.columns([1, 1])
run_btn = _col_run.button("Build graph", type="primary")
unlock_btn = _col_unlock.button(
    "Unlock (clear stuck lock)",
    help="Force-clear the per-namespace lock left behind by a hard-killed/cancelled run "
         "(Ctrl-C / OOM / docker stop). ONLY use when no build for this project is actually "
         "running — otherwise a second writer could start concurrently.",
)

status_box = st.empty()

if unlock_btn:
    _apply_neo4j_env(neo4j_uri, neo4j_user, neo4j_password, neo4j_database)
    from graph_core.store import GraphStore
    from graph_core.config import neo4j_config
    from ingest.indexing import slugify_project

    ns = slugify_project(project)
    _store = GraphStore(neo4j_config())
    try:
        existed = _store.force_release_lock(ns)
        if existed:
            status_box.success(f"Lock cleared for namespace '{ns}' — you can rebuild now.")
        else:
            status_box.info(f"No GraphMeta node for '{ns}' — nothing to unlock (already free).")
    except Exception as exc:
        status_box.error(f"Unlock failed: {type(exc).__name__}: {exc}")
    finally:
        _store.close()

if run_btn:
    if uploaded is None and not zip_path_input:
        st.error("Provide a zip (upload or local path).")
    elif st.session_state.get("build") is not None:
        st.warning("A build is already running — stop it before starting another.")
    else:
        _apply_neo4j_env(neo4j_uri, neo4j_user, neo4j_password, neo4j_database)
        os.environ["GRAPH_WRITE_BATCH_SIZE"] = str(int(write_batch_size))
        os.environ["GRAPH_WRITE_WORKERS"] = str(int(write_workers))

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
            javac=javac,
            javac_timeout_seconds=float(javac_timeout),
            javac_batch_size=int(javac_batch),
            bytecode=bytecode,
            bytecode_class_roots=bytecode_roots.strip() or None,
            extract_cache=extract_cache,
            extract_cache_dir=extract_cache_dir,
            cache_io_workers=int(cache_io_workers),
            zip_extract_workers=int(zip_extract_workers),
            write_workers=int(write_workers),
        )

        if uploaded is not None:
            tmp_dir = tempfile.mkdtemp(prefix="streamlit_upload_")
            tmp_zip_path = os.path.join(tmp_dir, uploaded.name)
            with open(tmp_zip_path, "wb") as fh:
                fh.write(uploaded.getbuffer())
        else:
            tmp_zip_path = zip_path_input

        # Non-blocking: run the build on a worker thread with a cancel Event, stash
        # it in session_state, and let reruns drive the timer so the STOP button
        # stays clickable (a blocking poll loop would freeze the whole UI).
        cancel_event = threading.Event()
        result_holder: dict = {}
        stage_state = {"text": "starting…"}

        def _on_stage(stage, detail):
            stage_state["text"] = f"{stage} — {detail}"

        def _worker():
            try:
                result_holder["value"] = build_graph_from_zip(
                    tmp_zip_path, project,
                    on_stage=_on_stage, cancel_check=cancel_event.is_set,
                    sample_interval_s=sample_interval, runs_dir="runs",
                )
            except Exception as exc:  # includes IngestCancelled on STOP
                result_holder["error"] = exc

        worker = threading.Thread(target=_worker, name="graph-build", daemon=True)
        worker.start()
        st.session_state["build"] = {
            "thread": worker, "result": result_holder, "start": time.time(),
            "cancel": cancel_event, "stage": stage_state, "project": project,
        }
        st.rerun()

# --- Non-blocking build monitor: runs on every rerun; renders the live timer +
# RAM + a working STOP button while the worker thread runs. ---
_build = st.session_state.get("build")
if _build is not None:
    _alive = _build["thread"].is_alive()
    _elapsed = time.time() - _build["start"]
    _rss = _current_rss_mb()
    _color = "#2563eb" if _alive else "#16a34a"
    st.markdown(
        f"<div style='font-size:5rem;font-weight:700;line-height:1;"
        f"font-variant-numeric:tabular-nums;color:{_color}'>⏱️ {_fmt_elapsed(_elapsed)}</div>"
        f"<div style='font-size:1.15rem;color:#64748b;font-variant-numeric:tabular-nums'>"
        f"RAM {_rss:,.0f} MB</div>",
        unsafe_allow_html=True,
    )
    if _alive:
        status_box.info(f"Stage: {_build['stage']['text']}")
        if st.button("🛑 STOP build", type="secondary"):
            _build["cancel"].set()
            status_box.warning(
                "Stop requested — cancelling at the next checkpoint (resolve); the "
                "namespace lock is released automatically once it stops. If it's wedged "
                "in a non-cancellable phase, use `docker stop` / Ctrl-C, then Unlock."
            )
        time.sleep(0.5)
        st.rerun()
    else:
        _res = _build["result"]
        if "error" in _res:
            _exc = _res["error"]
            status_box.error(f"Stopped/failed: {type(_exc).__name__}: {_exc}")
        elif "value" in _res:
            _report = _res["value"]
            status_box.success(f"Done in {_report['total_duration_s']}s — peak {_report['overall_peak_mb']} MB")

            # Which precise tier actually covered what. A silent fallback to the
            # heuristic looks identical to success in node/edge counts, so the
            # availability flag and its reason are surfaced first.
            _bc = (_report.get("result_counts") or {}).get("bytecode") or {}
            if _bc:
                st.subheader("Bytecode resolver")
                if _bc.get("available"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("File coverage", f"{100 * _bc['file_coverage']:.1f}%",
                              help=f"{_bc['attributed_file_count']} of "
                                   f"{_bc['java_files_seen']} .java files")
                    c2.metric("Match rate", f"{100 * _bc['match_rate']:.1f}%",
                              help="Bytecode methods pinned to a source node. A low "
                                   "value means the class files are STALE relative "
                                   "to the source.")
                    c3.metric("CALLS", f"{_bc['call_edges']:,}",
                              help="Exact compiler bindings — nothing inferred.")
                    c4.metric("READS/WRITES", f"{_bc['field_edges']:,}",
                              help="Field access with the exact owning class, not "
                                   "just explicit `this.x`.")
                    c5, c6, c7, c8 = st.columns(4)
                    c5.metric("Synthesized nodes", f"{_bc['synthesized_nodes']:,}",
                              help="Lambdas, anonymous inner classes and static "
                                   "initializers — constructs with no source "
                                   "declaration for tree-sitter to find.")
                    c6.metric("Classes in repo", f"{_bc['classes_in_repo']:,}",
                              help=f"of {_bc['classes_seen']:,} class files read")
                    c7.metric("External calls", f"{_bc['external_calls']:,}",
                              help="Callee outside the repo — no in-repo node can "
                                   "be the target.")
                    c8.metric("Seconds", f"{_bc['seconds']:.1f}")
                    if _bc.get("synthesis_skipped_no_lines"):
                        st.warning(
                            f"{_bc['synthesis_skipped_no_lines']} construct(s) skipped "
                            "for lacking a LineNumberTable — compile with `-g` to "
                            "capture lambdas/anonymous classes there. A node without "
                            "real positions is never created."
                        )
                    with st.expander("Match detail"):
                        st.json(_bc.get("match_stats") or {})
                else:
                    st.info(f"Not used — {_bc.get('reason') or 'disabled'}. "
                            "Java stayed on javac/heuristic.")

            st.subheader("Stages")
            st.dataframe(_report["stages"])
            st.subheader("Full report")
            st.json(_report)
        else:
            status_box.warning("Build thread ended without a result.")
        del st.session_state["build"]

st.divider()
st.caption("Past runs: see runs/index.jsonl for a summary of every run, runs/<id>.json for full detail.")

"""Stage 2 (precise) — resolve CALLS from a SCIP index instead of name heuristics.

We run a language-specific SCIP indexer over the repo (scip-python, Pyright-
backed, for Python; scip-java, Maven/Gradle-build-backed, for Java), which
emits a SCIP index: for every identifier *occurrence* it records the global
symbol it refers to and whether that occurrence is a definition / read /
write / import. That gives us type-precise, cross-file resolution that the
name-matching heuristic can only guess at (measured for Python: the heuristic
was ~85% precise / ~93% recall vs SCIP).

How we map SCIP symbols onto our graph, robustly and without parsing SCIP's
descriptor sigils, is identical for both languages (`resolve_edges` below is
language-agnostic, only the indexer invocation differs):

  1. A SCIP symbol's *definition occurrence* gives `(file, line)`. Our nodes
     already carry `(file, start_line)`. Match on location  ->  `symbol -> node`.
  2. A non-definition occurrence of a function symbol is a **call**. Its line,
     placed against our Function line-ranges, gives the enclosing call site.
     Emit `CALLS(call_site -> target)` at EXTRACTED confidence.

Only CALLS + OVERRIDES are taken over here for now; the heuristic still owns
EXTENDS / IMPLEMENTS / INSTANTIATES / ANNOTATED_WITH / IMPORTS / AUTOWIRED for
both languages. (READS/WRITES are a natural extension — SCIP tags reads, but
neither indexer build here emits WriteAccess, and we don't yet model instance
fields as nodes for Python, so that waits.)

scip-java specifically needs a working Maven/Gradle build (it compiles the
project with a semanticdb-javac plugin attached to get type info), so it is
only attempted when a build file (pom.xml / build.gradle[.kts]) is present at
the repo root, and any failure (missing binary, no JDK, build failure, no
network to fetch deps) degrades gracefully to the heuristic resolver, exactly
like the scip-python path.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .config import (
    scip_java_bin,
    scip_java_max_files,
    scip_java_timeout_seconds,
    scip_python_bin,
)
from .models import Confidence, Edge, Node, Origin
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# SCIP SymbolRole bitmask (from scip.proto)
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8


@dataclass
class ScipReport:
    available: bool = False           # was a SCIP index produced/parsed?
    tool: str = ""                    # indexer name+version
    documents: int = 0
    inrepo_defs: int = 0              # in-repo symbols with a definition
    mapped_defs: int = 0             # ... that matched one of our nodes
    calls: int = 0                    # distinct CALLS edges emitted
    overrides: int = 0                # distinct OVERRIDES edges emitted


def run_scip_python(repo_root: str, project_name: str, out_path: str) -> str | None:
    """Run scip-python with cwd=repo_root (it discovers files relative to cwd).
    Returns the index path on success, else None."""
    binpath = scip_python_bin()
    if not binpath:
        _log.info("[scip] scip-python: no binary found on PATH/$SCIP_PYTHON_BIN — falling back to heuristic resolver")
        return None
    _log.info("[scip][repo=%s] scip-python: starting (%s)", project_name, binpath)
    t0 = time.time()
    try:
        subprocess.run(
            # --project-version is required: without it scip-python defaults the
            # version to `git rev-parse` and crashes on a non-git directory
            # (e.g. a vendored/extracted source tree). The value is not used in
            # our definition-location mapping, so a static placeholder is fine.
            [binpath, "index", "--project-name", project_name,
             "--project-version", "0.0.0",
             "--output", os.path.abspath(out_path), "--quiet"],
            cwd=os.path.abspath(repo_root),
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        _log.warning(
            "[scip][repo=%s] scip-python: failed after %.3fs (exit=%s) — falling back to heuristic. stderr: %s",
            project_name, time.time() - t0, exc.returncode, (exc.stderr or "")[:500],
        )
        return None
    except OSError as exc:
        _log.warning("[scip][repo=%s] scip-python: failed to launch: %s — falling back to heuristic", project_name, exc)
        return None
    ok = os.path.exists(out_path)
    _log.info("[scip][repo=%s] scip-python: finished in %.3fs (index produced=%s)", project_name, time.time() - t0, ok)
    return out_path if ok else None


_JAVA_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")


def has_java_build_tool(repo_root: str) -> str | None:
    """Detect a Maven/Gradle build at the repo root (multi-module repos keep
    their root pom.xml/build.gradle here too). Returns "maven"/"gradle", or
    None if neither is present. scip-java needs one to resolve dependencies
    and compile with its semanticdb-javac plugin, so we skip attempting it
    entirely rather than shelling out to a doomed indexer run."""
    if os.path.exists(os.path.join(repo_root, "pom.xml")):
        return "maven"
    if os.path.exists(os.path.join(repo_root, "build.gradle")) or os.path.exists(
        os.path.join(repo_root, "build.gradle.kts")
    ):
        return "gradle"
    return None


# Fallback only — the live value comes from config.scip_java_timeout_seconds()
# (env SCIP_JAVA_TIMEOUT_SECONDS), read at job-start time so it can be sized to
# the target repo's actual build. Kept as a module constant because it is the
# documented default and several signatures below default to it.
SCIP_JAVA_TIMEOUT_SECONDS = 900.0


def _run_subprocess_with_heartbeat(
    cmd: list[str], cwd: str, timeout: float,
    on_heartbeat: "Callable[[float, float], None] | None" = None,
    heartbeat_interval: float = 10.0,
) -> bool:
    """Like subprocess.run(..., timeout=...) but polls instead of blocking
    silently, so on_heartbeat(elapsed_s, timeout_s) can fire periodically —
    scip-java compiling a large repo can otherwise run up to `timeout` with
    zero signal that anything is happening. Discards stdout/stderr, matching
    subprocess.run's previous behavior here (failures were never surfaced
    before this change either, just swallowed by the bare except)."""
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    start = time.time()
    while True:
        try:
            proc.wait(timeout=heartbeat_interval)
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            if elapsed >= timeout:
                proc.kill()
                proc.wait()
                return False
            if on_heartbeat:
                on_heartbeat(elapsed, timeout)


def run_scip_java(repo_root: str, out_path: str, build_tool: str,
                   on_heartbeat: "Callable[[float, float], None] | None" = None,
                   timeout: float | None = None) -> str | None:
    """Run scip-java with cwd=repo_root. Requires a working Maven/Gradle build
    (it compiles the project via a semanticdb-javac plugin to get type info),
    so this is only worth calling when `has_java_build_tool` found one.
    Returns the index path on success, else None (caller falls back to the
    heuristic resolver) - any subprocess/tooling failure here (missing JDK,
    dependency resolution needing network access, compile errors) is expected
    to be common in constrained/offline environments and is not fatal."""
    binpath = scip_java_bin()
    if not binpath:
        return None
    # None (the default) means "read the configured budget now" — resolving it
    # at call time rather than at import time so the env var applies.
    if timeout is None:
        timeout = scip_java_timeout_seconds()
    ok = _run_subprocess_with_heartbeat(
        [binpath, "index", "--build-tool", build_tool, "--output", os.path.abspath(out_path)],
        cwd=os.path.abspath(repo_root), timeout=timeout, on_heartbeat=on_heartbeat,
    )
    return out_path if ok and os.path.exists(out_path) else None


@dataclass
class ScipJavaJob:
    """A scip-java compile started early (non-blocking) so it can run
    concurrently with other work (Python-side extraction); join it with
    finish_scip_java_job() once you actually need the resolved edges."""
    proc: subprocess.Popen
    out_path: str
    started_at: float
    timeout: float = SCIP_JAVA_TIMEOUT_SECONDS


def start_scip_java_job(repo_root: str, java_file_count: int) -> "ScipJavaJob | None":
    """Kick off scip-java compiling in the background. Returns None (nothing
    started — caller stays on the heuristic resolver) if there's no
    Maven/Gradle build, no binary, or the repo exceeds SCIP_JAVA_MAX_FILES —
    skip attempting a doomed or too-costly compile rather than starting it."""
    build_tool = has_java_build_tool(repo_root)
    if build_tool is None:
        _log.info("[scip] scip-java: no Maven/Gradle build file found — staying on heuristic resolver for Java")
        return None
    binpath = scip_java_bin()
    if not binpath:
        _log.info("[scip] scip-java: no binary found on PATH/$SCIP_JAVA_BIN — staying on heuristic resolver for Java")
        return None
    max_files = scip_java_max_files()
    if max_files and java_file_count > max_files:
        _log.info(
            "[scip] scip-java: %s Java file(s) exceeds SCIP_JAVA_MAX_FILES=%s — skipping (too costly), staying on heuristic",
            java_file_count, max_files,
        )
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".scip", delete=False)
    tmp.close()
    timeout = scip_java_timeout_seconds()
    _log.info(
        "[scip] scip-java: starting compile in background (%s, %s Java file(s), timeout %.0fs)",
        build_tool, java_file_count, timeout,
    )
    try:
        proc = subprocess.Popen(
            [binpath, "index", "--build-tool", build_tool, "--output", os.path.abspath(tmp.name)],
            cwd=os.path.abspath(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _log.warning("[scip] scip-java: failed to launch: %s — staying on heuristic resolver for Java", exc)
        os.unlink(tmp.name)
        return None
    return ScipJavaJob(proc=proc, out_path=tmp.name, started_at=time.time(), timeout=timeout)


def finish_scip_java_job(nodes: list[Node], job: "ScipJavaJob | None", project_name: str,
                          on_heartbeat: "Callable[[float, float], None] | None" = None,
                          heartbeat_interval: float = 10.0):
    """Join a job started by start_scip_java_job, then resolve edges (same
    return shape as scip_resolve_java). Safe to call with job=None (nothing
    was started) — returns the same "unavailable" report as any other
    scip-java failure so the pipeline falls back to the heuristic resolver."""
    if job is None:
        return [], ScipReport(available=False)
    try:
        while True:
            try:
                job.proc.wait(timeout=heartbeat_interval)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.time() - job.started_at
                if elapsed >= job.timeout:
                    _log.warning(
                        "[scip][repo=%s] scip-java: timed out after %.1fs (limit %.1fs) — killing, falling back to heuristic",
                        project_name, elapsed, job.timeout,
                    )
                    job.proc.kill()
                    job.proc.wait()
                    return [], ScipReport(available=False)
                if on_heartbeat:
                    on_heartbeat(elapsed, job.timeout)
        elapsed_total = time.time() - job.started_at
        if job.proc.returncode != 0 or not os.path.exists(job.out_path):
            _log.warning(
                "[scip][repo=%s] scip-java: finished in %.3fs with exit=%s (index produced=%s) — falling back to heuristic",
                project_name, elapsed_total, job.proc.returncode, os.path.exists(job.out_path),
            )
            return [], ScipReport(available=False)
        _log.info("[scip][repo=%s] scip-java: compile finished in %.3fs — resolving edges", project_name, elapsed_total)
        return resolve_edges(nodes, job.out_path, project_name, tool_name="scip-java")
    finally:
        if os.path.exists(job.out_path):
            os.unlink(job.out_path)


def _is_inrepo(symbol: str, project_name: str) -> bool:
    """SCIP symbol = `<scheme> <manager> <pkg> <version> <descriptor>`; in-repo
    symbols carry the project package. `local N` symbols are intra-function."""
    if symbol.startswith("local "):
        return False
    parts = symbol.split(" ", 4)
    return len(parts) >= 5 and parts[2] == project_name


def resolve_edges(nodes: list[Node], index_path: str, project_name: str,
                   tool_name: str = "scip"):
    """Parse a SCIP index -> (EXTRACTED edges [CALLS + OVERRIDES], ScipReport).
    Language-agnostic: works the same for a scip-python or scip-java index,
    since both emit the same SCIP protobuf schema. `tool_name` only tags the
    emitted edges' `extractor` field for provenance."""
    from .scip import scip_pb2  # lazy: requires the `protobuf` runtime

    index = scip_pb2.Index()
    with open(index_path, "rb") as fh:
        index.ParseFromString(fh.read())

    report = ScipReport(
        available=True,
        tool=f"{index.metadata.tool_info.name} {index.metadata.tool_info.version}".strip(),
        documents=len(index.documents),
    )

    # Our nodes, indexed for location lookup and enclosing-function lookup.
    node_by_loc: dict[tuple[str, int], Node] = {}
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for n in nodes:
        if n.label in ("Function", "Class"):
            node_by_loc[(n.file, n.start_line)] = n
        if n.label == "Function":
            spans[n.file].append((n.start_line, n.end_line, n.id))
    for v in spans.values():
        v.sort()
    def enclosing_function(relpath: str, line1: int) -> str | None:
        found = None
        for st, en, nid in spans.get(relpath, []):
            if st <= line1 <= en:
                found = nid  # sorted by start -> last match is innermost
            elif st > line1:
                break
        return found

    # 1. symbol -> node, via each in-repo symbol's definition occurrence.
    sym_to_node: dict[str, Node] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION and _is_inrepo(occ.symbol, project_name):
                if occ.symbol in sym_to_node:
                    continue
                report.inrepo_defs += 1
                node = node_by_loc.get((doc.relative_path, occ.range[0] + 1))
                if node is not None:
                    sym_to_node[occ.symbol] = node
                    report.mapped_defs += 1

    # 2. non-definition occurrences of a function symbol = calls. Keep one
    #    representative evidence location (file:line:col) per distinct edge.
    calls: dict[tuple[str, str], tuple[str, int, int]] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION:
                continue
            target = sym_to_node.get(occ.symbol)
            if target is None or target.label != "Function":
                continue
            src = enclosing_function(doc.relative_path, occ.range[0] + 1)
            if src and src != target.id:
                calls.setdefault(
                    (src, target.id),
                    (doc.relative_path, occ.range[0] + 1, occ.range[1]),
                )

    edges = [
        Edge("CALLS", s, d, Confidence.EXTRACTED.value,
             origin=Origin.EXTRACTED.value, extractor=tool_name,
             evidence_file=ev[0], evidence_line=ev[1], evidence_col=ev[2])
        for (s, d), ev in calls.items()
    ]
    report.calls = len(edges)

    # 3. OVERRIDES — a method's SymbolInformation lists the base methods it
    #    implements/overrides (Relationship.is_implementation). Both must be in-repo.
    overrides: dict[tuple[str, str], Node] = {}
    for doc in index.documents:
        for si in doc.symbols:
            src_node = sym_to_node.get(si.symbol)
            if src_node is None or src_node.label != "Function":
                continue
            for rel in si.relationships:
                if not rel.is_implementation:
                    continue
                tgt = sym_to_node.get(rel.symbol)
                if tgt is not None and tgt.label == "Function" and tgt.id != src_node.id:
                    overrides.setdefault((src_node.id, tgt.id), src_node)
    for (s, d), sn in overrides.items():
        edges.append(Edge(
            "OVERRIDES", s, d, Confidence.EXTRACTED.value,
            origin=Origin.EXTRACTED.value, extractor=tool_name,
            evidence_file=sn.file, evidence_line=sn.start_line, evidence_col=sn.start_col))
    report.overrides = len(overrides)
    _log.info(
        "[scip][tool=%s] resolve_edges: %s document(s), %s in-repo def(s) (%s mapped), %s CALLS, %s OVERRIDES",
        tool_name, report.documents, report.inrepo_defs, report.mapped_defs, report.calls, report.overrides,
    )
    return edges, report


def scip_resolve(nodes: list[Node], repo_root: str, project_name: str,
                 index_path: str | None = None):
    """High-level entry: ensure a SCIP index exists, then resolve CALLS.

    If `index_path` is given it is reused; otherwise scip-python is run into a
    temp file. Returns (edges, ScipReport). On any failure returns ([], report
    with available=False) so the pipeline can fall back to the heuristic.
    """
    tmp = None
    try:
        if index_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".scip", delete=False)
            tmp.close()
            index_path = run_scip_python(repo_root, project_name, tmp.name)
            if index_path is None:
                return [], ScipReport(available=False)
        return resolve_edges(nodes, index_path, project_name, tool_name="scip-python")
    finally:
        if tmp is not None and os.path.exists(tmp.name):
            os.unlink(tmp.name)


def scip_resolve_java(nodes: list[Node], repo_root: str, project_name: str,
                      index_path: str | None = None):
    """Same as `scip_resolve` but for Java via scip-java. Returns ([], report
    with available=False) immediately (no subprocess attempt) if no Maven/
    Gradle build file is present, since scip-java cannot resolve dependencies
    or compile without one.

    Synchronous convenience wrapper (start + immediately finish). Prefer
    start_scip_java_job()/finish_scip_java_job() directly when the compile
    should overlap with other work (see pipeline.index_repo)."""
    if index_path is not None:
        return resolve_edges(nodes, index_path, project_name, tool_name="scip-java")
    java_file_count = sum(1 for n in nodes if getattr(n, "lang", None) == "java")
    job = start_scip_java_job(repo_root, java_file_count)
    return finish_scip_java_job(nodes, job, project_name)

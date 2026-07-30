"""Precise Java CALLS edges from javac itself, replacing heuristic name matching.

WHY
Measured against javac ground truth on a 16.5k-file Java repo, the heuristic
resolver scored 93.8% recall but 5.0% precision — it finds the right target
almost always, then emits every same-named candidate alongside it because it
has no type system to choose with. Ambiguous CALLS specifically measured 0.1%
precision (1,928 correct out of 3.3M) while contributing 1.0% recall.

javac has the type system. This module runs it over the same source tree and
turns its resolved bindings into edges, so Java CALLS become compiler-accurate
instead of name-guessed.

WHY IT WORKS WITHOUT A BUILD SYSTEM
scip-java is unusable here: it compiles via Maven/Gradle, and the ingested tree
is source-only (the upload path strips pom.xml, and the repo may genuinely have
no build files). javac does NOT need that. With `-sourcepath <root>` it resolves
every in-repo type from source alone; only external types (JDK, third-party
jars) fail, and those are reported as errors and skipped. Since the graph only
ever contains in-repo targets, partial attribution is exactly sufficient.

SCOPE
Java CALLS only. Every other edge type, and every other language, stays on the
heuristic resolver — this replaces the one thing it is worst at.

FALLBACK
Any failure (no JDK, compile abort, timeout, low coverage) returns
``available=False`` and the caller keeps the heuristic edges, exactly as the
SCIP path does. Never raises into the pipeline.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from .models import Confidence, Edge, Node, Origin
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Ships alongside this package; compiled to a temp dir on first use.
_ORACLE_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "oracle", "CallOracle.java",
)


@dataclass
class JavacReport:
    available: bool = False
    reason: str = ""
    rows: int = 0                 # invocations javac resolved to in-repo targets
    edges: int = 0                # rows successfully mapped onto graph nodes
    unmatched_caller: int = 0     # row's caller not found among graph nodes
    unmatched_callee: int = 0
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)   # the oracle's own STATS block


def javac_available() -> tuple[bool, str]:
    """(ok, reason) — both javac and java must be on PATH, plus the oracle source."""
    if not shutil.which("javac"):
        return False, "javac not on PATH (a JDK is required, a JRE is not enough)"
    if not shutil.which("java"):
        return False, "java not on PATH"
    if not os.path.isfile(_ORACLE_SRC):
        return False, f"oracle source missing at {_ORACLE_SRC}"
    return True, ""


def _compile_oracle(workdir: str) -> str | None:
    out = os.path.join(workdir, "classes")
    os.makedirs(out, exist_ok=True)
    try:
        proc = subprocess.run(
            ["javac", "-d", out, _ORACLE_SRC],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("[javac] failed to launch javac: %s", exc)
        return None
    if proc.returncode != 0:
        _log.warning("[javac] oracle compile failed: %s", (proc.stderr or "")[:500])
        return None
    return out


def _parse_stats(stderr: str) -> dict:
    """Pull the oracle's `=== STATS ===` key/value block off stderr."""
    stats: dict = {}
    seen = False
    for line in (stderr or "").splitlines():
        if line.startswith("=== STATS ==="):
            seen = True
            continue
        if seen:
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                stats[parts[0]] = int(parts[1])
    return stats


def _norm_method(cls_fqn: str, method: str) -> str:
    """javac reports constructors as `<init>`; the Java extractor names them
    after their class (java.py: `class_fqn.rsplit('.',1)[-1]` when is_ctor).
    Normalize to the graph's convention so both sides key identically."""
    if method == "<init>":
        return cls_fqn.rsplit(".", 1)[-1]
    return method


def resolve_java_calls(
    nodes: list[Node], repo_root: str, repo: str,
    timeout: float = 3600.0, batch_size: int = 400,
    min_rows: int = 1,
) -> tuple[list[Edge], JavacReport]:
    """Run the oracle and map its bindings onto graph node ids.

    ``nodes`` must contain the Function nodes already extracted for this repo —
    the oracle emits fully-qualified names, and those are matched to node ids
    via (fqn, param_count). Returns ([], unavailable-report) on any failure so
    the caller can keep its heuristic edges.

    ``min_rows`` guards against a run that "succeeded" but attributed almost
    nothing (e.g. the tree failed to resolve): replacing a working heuristic
    graph with a near-empty one would be worse than not running at all.
    """
    rep = JavacReport()
    ok, why = javac_available()
    if not ok:
        rep.reason = why
        _log.info("[javac] %s — staying on heuristic resolver for Java", why)
        return [], rep

    t0 = time.monotonic()
    workdir = tempfile.mkdtemp(prefix="javac_oracle_")
    try:
        classes = _compile_oracle(workdir)
        if classes is None:
            rep.reason = "oracle compile failed"
            return [], rep

        tsv_path = os.path.join(workdir, "calls.tsv")
        _log.info(
            "[javac] running call oracle over %s (timeout %.0fs, batch %s)",
            repo_root, timeout, batch_size,
        )
        try:
            with open(tsv_path, "w", encoding="utf-8") as fh:
                proc = subprocess.run(
                    ["java", "-cp", classes, "CallOracle",
                     os.path.abspath(repo_root), str(batch_size)],
                    stdout=fh, stderr=subprocess.PIPE, text=True, timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            rep.reason = f"oracle timed out after {timeout:.0f}s"
            _log.warning("[javac] %s — staying on heuristic resolver for Java", rep.reason)
            return [], rep
        except (OSError, subprocess.SubprocessError) as exc:
            rep.reason = f"oracle failed to run: {exc}"
            return [], rep

        rep.stats = _parse_stats(proc.stderr)
        if proc.returncode != 0:
            rep.reason = f"oracle exited {proc.returncode}"
            _log.warning("[javac] %s — staying on heuristic resolver for Java", rep.reason)
            return [], rep

        # (fqn, param_count) -> node id. Overloads share an fqn and are told
        # apart by arity, which is exactly what the oracle emits for both ends.
        by_key: dict[tuple[str, int], str] = {}
        for n in nodes:
            if n.label == "Function" and n.fqn:
                by_key.setdefault((n.fqn, n.param_count or 0), n.id)

        edges: list[Edge] = []
        seen: set[tuple[str, str]] = set()
        with open(tsv_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 8:
                    continue
                (caller_cls, caller_m, caller_ar,
                 callee_cls, callee_m, callee_ar, file, ln) = parts[:8]
                rep.rows += 1
                try:
                    caller_arity = int(caller_ar)
                    callee_arity = int(callee_ar)
                    line_no = int(ln)
                except ValueError:
                    continue

                caller_fqn = f"{caller_cls}#{_norm_method(caller_cls, caller_m)}"
                callee_fqn = f"{callee_cls}#{_norm_method(callee_cls, callee_m)}"
                src = by_key.get((caller_fqn, caller_arity))
                if src is None:
                    rep.unmatched_caller += 1
                    continue
                dst = by_key.get((callee_fqn, callee_arity))
                if dst is None:
                    rep.unmatched_callee += 1
                    continue
                if (src, dst) in seen:
                    continue
                seen.add((src, dst))
                edges.append(Edge(
                    "CALLS", src, dst,
                    # EXTRACTED, not INFERRED: this is a compiler binding, not a
                    # heuristic guess. Nothing about it is uncertain.
                    Confidence.EXTRACTED.value,
                    origin=Origin.EXTRACTED.value,
                    extractor="javac",
                    evidence_file=file,
                    evidence_line=line_no,
                    strategy="javac_typed",
                ))

        rep.edges = len(edges)
        rep.seconds = time.monotonic() - t0
        if rep.edges < min_rows:
            rep.reason = f"only {rep.edges} edge(s) mapped — too thin to trust"
            _log.warning("[javac] %s — staying on heuristic resolver for Java", rep.reason)
            return [], rep

        rep.available = True
        _log.info(
            "[javac] resolved %s in-repo call(s) -> %s edge(s) in %.1fs "
            "(unmatched caller=%s callee=%s) stats=%s",
            rep.rows, rep.edges, rep.seconds,
            rep.unmatched_caller, rep.unmatched_callee, rep.stats,
        )
        return edges, rep
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

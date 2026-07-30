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
Hard failures (no JDK, compile abort, timeout, broken attribution) return
``available=False`` and the caller keeps every heuristic edge. Never raises into
the pipeline.

PARTIAL COVERAGE IS NOT A FAILURE
javac may attribute only part of a tree. The caller passes
``report.attributed_files`` to ``resolve(skip_call_files=...)``, so the heuristic
still resolves CALLS for every file javac missed. Without that, an all-or-nothing
takeover would leave unattributed files with NO call edges — worse than
name-matched ones, and invisible in the output. The graph can therefore hold both
provenances at once; ``r.strategy == 'javac_typed'`` distinguishes them.
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
    # Repo-relative paths javac actually attributed. THE critical field: the
    # heuristic must keep resolving CALLS for every Java file NOT in here, or
    # those files silently end up with no call edges at all. Coverage is
    # per-file rather than all-or-nothing precisely so partial attribution
    # degrades gracefully instead of falling off a threshold cliff.
    attributed_files: set[str] = field(default_factory=set)
    # Distinct files, counted separately because attributed_files deliberately
    # holds BOTH separator spellings of each path (see the @FILE handling) and
    # len() would therefore double-count on Windows.
    attributed_file_count: int = 0
    java_files_seen: int = 0      # .java files the pipeline handed over
    attribution_rate: float = 0.0  # bound invocations / invocations seen

    @property
    def file_coverage(self) -> float:
        if not self.java_files_seen:
            return 0.0
        return self.attributed_file_count / self.java_files_seen


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
    java_files_seen: int = 0,
    min_attribution_rate: float = 0.5,
) -> tuple[list[Edge], JavacReport]:
    """Run the oracle and map its bindings onto graph node ids.

    ``nodes`` must contain the Function nodes already extracted for this repo —
    the oracle emits fully-qualified names, and those are matched to node ids
    via (fqn, param_count). Returns ([], unavailable-report) on any failure so
    the caller can keep its heuristic edges.

    ``java_files_seen``: how many .java files the pipeline is indexing. Used
    only to report file coverage; it does NOT gate anything, because the caller
    is expected to hand ``report.attributed_files`` to resolve() and let the
    heuristic cover whatever javac did not. Partial coverage is a normal,
    safe outcome — not a failure.

    ``min_attribution_rate``: the one real quality floor. If javac bound less
    than this fraction of the invocations it saw, attribution itself was broken
    (missing sources, cascading unresolved symbols) rather than merely partial,
    and its edges would be unreliable even for the files it "attributed" — so
    the whole pass is abandoned. Distinct from coverage: a run can attribute
    30% of files perfectly (fine, use it for those) or 100% of files badly
    (not fine, discard).
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
        rep.java_files_seen = java_files_seen
        with open(tsv_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if parts and parts[0] == "@FILE":
                    # Attributed-file marker (see CallOracle). Recorded even for
                    # files with no calls — coverage is about what javac RESOLVED,
                    # not about what happened to contain an invocation.
                    #
                    # BOTH separator forms are stored. The oracle emits POSIX
                    # paths; discovery.FileInfo.relpath (and therefore
                    # RawRef.ref_file) uses os.sep, so on Windows these are
                    # 'a/b/C.java' vs 'a\\b\\C.java' and the membership test in
                    # resolve() silently never matches — javac would appear to
                    # work while the heuristic quietly re-resolved everything.
                    # Platform-dependent no-op, so it is normalized here, once,
                    # rather than per-ref in the hot loop.
                    if len(parts) > 1 and parts[1]:
                        rel = parts[1]
                        if rel not in rep.attributed_files:
                            rep.attributed_file_count += 1
                        rep.attributed_files.add(rel)
                        rep.attributed_files.add(rel.replace("/", os.sep))
                    continue
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

        # Attribution QUALITY floor. `unresolved` counts invocations javac could
        # not bind at all; a high share means attribution broke rather than
        # merely covering part of the tree, and even the "attributed" files
        # cannot be trusted. Coverage is deliberately NOT gated here — the
        # caller uses attributed_files so uncovered files keep their heuristic
        # edges, which makes partial coverage safe by construction.
        seen_inv = rep.stats.get("invocations_seen", 0)
        bound = rep.stats.get("resolved_in_repo", 0) + rep.stats.get("resolved_external", 0)
        rep.attribution_rate = (bound / seen_inv) if seen_inv else 0.0
        if seen_inv and rep.attribution_rate < min_attribution_rate:
            rep.reason = (
                f"attribution rate {rep.attribution_rate:.0%} below "
                f"{min_attribution_rate:.0%} ({bound}/{seen_inv} invocations bound) "
                f"— javac could not resolve this tree reliably"
            )
            _log.warning("[javac] %s — staying on heuristic resolver for Java", rep.reason)
            return [], rep
        if not rep.attributed_files:
            rep.reason = "javac attributed no files"
            _log.warning("[javac] %s — staying on heuristic resolver for Java", rep.reason)
            return [], rep

        rep.available = True
        _log.info(
            "[javac] %s edge(s) from %s in-repo call(s) in %.1fs | "
            "files attributed %s/%s (%.0f%%) | attribution rate %.0f%% | "
            "unmatched caller=%s callee=%s | stats=%s",
            rep.edges, rep.rows, rep.seconds,
            rep.attributed_file_count, rep.java_files_seen or "?",
            100.0 * rep.file_coverage, 100.0 * rep.attribution_rate,
            rep.unmatched_caller, rep.unmatched_callee, rep.stats,
        )
        if rep.java_files_seen and rep.file_coverage < 0.95:
            # Not a failure — just must not pass silently, since the heuristic
            # is now responsible for the remainder and anyone reading edge
            # counts needs to know the graph has two provenances.
            _log.warning(
                "[javac] %s of %s Java file(s) were NOT attributed — those keep "
                "heuristic (name-matched) CALLS; the graph mixes both provenances "
                "(check r.strategy: 'javac_typed' vs the rest)",
                rep.java_files_seen - rep.attributed_file_count, rep.java_files_seen,
            )
        return edges, rep
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

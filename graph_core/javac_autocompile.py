"""Compile Java sources on demand when no class files exist.

WHY THIS IS SEPARATE FROM javac_resolver.py
javac_resolver shells out to javac to ask it questions about call bindings; this
module shells out to javac to PRODUCE class files so the bytecode resolver has
something to read. Different purpose, different failure handling: this one is a
convenience for corpora and test trees, and it must never take a build down.

WHEN IT RUNS
Only when the bytecode pass found no class sources at all AND the toggle is on. It
never overwrites, never touches an existing build output, and never competes with
real compiled artifacts — if a build already produced classes, this does nothing.

-g IS NOT OPTIONAL HERE
Compiled without debug info there is no LineNumberTable, and bytecode_resolver
refuses to synthesize a node without real line numbers (correctly — a node with
fabricated positions is worse than a missing one). Every lambda body, anonymous
inner class and static initializer would silently vanish. So this always passes
`-g`, and that is the single most important line in the file.

WHAT IT CANNOT DO
Resolve third-party dependencies. A repo whose sources need a classpath will fail
to compile here, and that failure is expected and non-fatal — real builds have real
classpaths, and reconstructing one is what Maven/Gradle exist for. This is for
self-contained trees: generated corpora, samples, and small services.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_MAX_SOURCES = 5000       # above this, a real build system should be doing the work
_TIMEOUT_SECONDS = 600.0


def is_enabled() -> bool:
    """GRAPH_JAVAC_AUTOCOMPILE=1/true. Off by default.

    Deliberately opt-in: silently compiling a customer's source tree is a
    surprising side effect, and a partial compile that half-succeeds would feed the
    bytecode resolver an incomplete picture that looks complete."""
    env = os.environ.get("GRAPH_JAVAC_AUTOCOMPILE", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def javac_available() -> bool:
    try:
        r = subprocess.run(["javac", "-version"], capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def collect_sources(root: str) -> list[str]:
    """Every .java under root, excluding build output so a previous run's copies
    are not recompiled into the new one."""
    skip = {"target", "build", "out", "bin", "dist", ".git", "classes",
            "node_modules", ".mvn"}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if name.endswith(".java"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def compile_tree(root: str, out_dir: str | None = None,
                 timeout: float = _TIMEOUT_SECONDS) -> tuple[str | None, dict]:
    """Compile every .java under root into out_dir. Returns (out_dir or None, report).

    Uses an @argfile rather than a command line: 101 paths already risks the
    Windows command-length limit, and a real tree blows past it outright.

    A partial compile is treated as FAILURE, not partial success. javac emits class
    files for whatever it managed before erroring, and handing those to the bytecode
    resolver produces a confidently incomplete graph — the same shape of problem as
    stale class files, which is the failure mode that whole pass is guarded against.
    """
    rep: dict = {"attempted": False, "sources": 0, "classes": 0,
                 "seconds": 0.0, "reason": ""}
    t0 = time.monotonic()

    if not javac_available():
        rep["reason"] = "javac not on PATH"
        _log.info("[autocompile] %s — skipping", rep["reason"])
        return None, rep

    sources = collect_sources(root)
    rep["sources"] = len(sources)
    if not sources:
        rep["reason"] = "no .java sources found"
        return None, rep
    if len(sources) > _MAX_SOURCES:
        rep["reason"] = (f"{len(sources)} sources exceeds the {_MAX_SOURCES} limit — "
                         f"use a real build and point GRAPH_BYTECODE_CLASS_ROOTS at it")
        _log.warning("[autocompile] %s", rep["reason"])
        return None, rep

    out_dir = out_dir or tempfile.mkdtemp(prefix="graph_autocompile_")
    os.makedirs(out_dir, exist_ok=True)
    rep["attempted"] = True

    argfile = os.path.join(out_dir, "_sources.txt")
    with open(argfile, "w", encoding="utf-8") as fh:
        for path in sources:
            # javac argfiles treat backslash as an escape, so POSIX-ify the paths.
            fh.write(path.replace("\\", "/") + "\n")

    cmd = [
        "javac",
        "-g",                       # LineNumberTable — see the module docstring
        "-nowarn",
        "-proc:none",               # skip annotation processing; we want class files
        "-d", out_dir,
        "-sourcepath", root,
        f"@{argfile}",
    ]
    _log.info("[autocompile] compiling %s source(s) with -g into %s",
              len(sources), out_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        rep["reason"] = f"javac timed out after {timeout}s"
        rep["seconds"] = time.monotonic() - t0
        _log.warning("[autocompile] %s", rep["reason"])
        return None, rep
    except OSError as exc:
        rep["reason"] = f"javac failed to launch: {exc}"
        rep["seconds"] = time.monotonic() - t0
        return None, rep

    rep["classes"] = sum(1 for dp, _dn, fn in os.walk(out_dir)
                         for f in fn if f.endswith(".class"))
    rep["seconds"] = time.monotonic() - t0

    if proc.returncode != 0:
        # Partial output is discarded on purpose — see the docstring.
        rep["reason"] = (f"javac exited {proc.returncode} "
                         f"({rep['classes']} class file(s) produced, discarded): "
                         f"{(proc.stderr or '')[:400]}")
        _log.warning(
            "[autocompile] compile FAILED — %s. Usually a missing classpath; a tree "
            "with third-party dependencies needs a real build.", rep["reason"],
        )
        return None, rep

    if not rep["classes"]:
        rep["reason"] = "javac succeeded but produced no class files"
        _log.warning("[autocompile] %s", rep["reason"])
        return None, rep

    _log.info("[autocompile] produced %s class file(s) from %s source(s) in %.1fs",
              rep["classes"], rep["sources"], rep["seconds"])
    return out_dir, rep


def ensure_class_roots(root: str, existing: list[str]) -> tuple[list[str], dict]:
    """Return class roots for the bytecode pass, compiling first if there are none.

    Called from pipeline.py before the bytecode stage. If `existing` already has
    anything, this is a no-op — a real build's output always wins over one this
    module would produce, because the real build has the right classpath.
    """
    if existing:
        return existing, {"attempted": False, "reason": "class sources already found"}
    if not is_enabled():
        return existing, {"attempted": False,
                          "reason": "GRAPH_JAVAC_AUTOCOMPILE not enabled"}
    out_dir, rep = compile_tree(root)
    return ([out_dir] if out_dir else []), rep

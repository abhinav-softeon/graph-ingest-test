"""Build script: compiles the resolve/derive hot-path modules with Cython.

Compiles graph_core/resolver.py and graph_core/pipeline.py AS-IS (no source
changes, no cdef type annotations added) into native extensions. This removes
Python bytecode-interpretation overhead from the two modules that dominate
ingest time per real profiling data (runs/b29bc9a9.json: resolve 38%, derive
42% of total wall time) — same algorithm, same data structures, same memory
layout, just compiled instead of interpreted.

Needs a C compiler (this project's own Dockerfile already has build-essential
on python:3.13-slim). Usage:
    pip install cython
    python setup_cython.py build_ext --inplace

Once built, the resulting graph_core/resolver*.so / pipeline*.so extension
modules are picked up automatically in place of the .py files — CPython's
import machinery prefers compiled extensions over source files with the same
module name, so no other code needs to change.
"""
from Cython.Build import cythonize
from pathlib import Path
from setuptools import setup


def _cython_targets() -> list[str]:
    """All eligible project modules to compile.

    Keeps this build script maintenance-free: any new module under the core
    runtime folders is picked up automatically without hand-editing this list.
    Excludes package markers and tests.
    """
    roots = ("graph_core", "ingest", "instrumentation", "sail_core")
    excluded_dirs = {"tests", "__pycache__"}
    targets: list[str] = []

    for root in roots:
        for path in Path(root).rglob("*.py"):
            if path.name == "__init__.py":
                continue
            if any(part in excluded_dirs for part in path.parts):
                continue
            targets.append(str(path).replace("\\", "/"))

    return sorted(targets)

setup(
    ext_modules=cythonize(
        _cython_targets(),
        compiler_directives={
            "language_level": "3",
            # Both files use PEP 526 variable annotations (`x: dict[...] = ...`)
            # purely as documentation, the normal Python convention — never
            # meant as enforced static types. Cython's default behavior turns
            # those into strict runtime type checks (e.g. rejecting a
            # defaultdict assigned to a `dict`-annotated name, since it isn't
            # an *exact* dict), which is a real behavior change from
            # interpreted Python, not just a speed difference. Off, so
            # compiled behavior matches interpreted Python exactly.
            "annotation_typing": False,
        },
    ),
)

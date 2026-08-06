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
from setuptools import setup

setup(
    ext_modules=cythonize(
        [
            # Core hot paths (original)
            "graph_core/resolver.py",
            "graph_core/pipeline.py",
            # Extractors
            "graph_core/extractors/common.py",
            "graph_core/extractors/java.py",
            "graph_core/extractors/javascript.py",
            "graph_core/extractors/jsp.py",
            "graph_core/extractors/python.py",
            "graph_core/extractors/sql.py",
            # Bytecode
            "graph_core/bytecode/classfile.py",
            "graph_core/bytecode/matcher.py",
            "graph_core/bytecode_resolver.py",
            # Graph core support
            "graph_core/discovery.py",
            "graph_core/ids.py",
            "graph_core/models.py",
            "graph_core/store.py",
            "graph_core/external_api.py",
            "graph_core/extract_cache.py",
            "graph_core/canonical_ir.py",
            "graph_core/checkpoint.py",
            # Ingest
            "ingest/build.py",
            "ingest/indexing.py",
            "ingest/upload_utils.py",
        ],
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

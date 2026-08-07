# Session Changes — 2026-08-07

Three files were changed to fix repeated runtime failures during ingest.

---

## 1. `requirements.txt`

**What changed**
- Pinned `tree-sitter` and all grammar packages to known-good exact versions (from `>=` ranges).
- Added `Cython==3.0.11` as an explicit build dependency.

**Why**
- `>=` ranges allowed pip to install newer versions (e.g. `tree-sitter 0.26.0`) whose native binaries were incompatible, causing `No module named 'tree_sitter._binding'` on every import.
- `Cython` was required by `setup_cython.py` but not listed, so fresh venv installs always failed at the build step.

**Before**
```
tree-sitter>=0.25.0
tree-sitter-python>=0.23.0
tree-sitter-javascript>=0.23.0
tree-sitter-typescript>=0.23.0
tree-sitter-java>=0.23.0
```

**After**
```
Cython==3.0.11
tree-sitter==0.25.1
tree-sitter-python==0.25.0
tree-sitter-javascript==0.25.0
tree-sitter-typescript==0.23.2
tree-sitter-java==0.23.5
```

---

## 2. `graph_core/pipeline.py` — `_apply_taint_marks()`

**What changed**
Added a per-node `hasattr` guard inside the taint stamping loop, plus a summary warning when stale nodes are skipped.

**Why**
- The Node dataclass gained `taint_categories`, `taint_source`, `taint_sites` fields.
- Old Node objects serialized in the extract cache or checkpoint files before those fields existed are loaded back during resume runs.
- When the taint loop touched one of those old objects it raised `AttributeError: 'Node' object has no attribute 'taint_categories'` and aborted the entire ingest — after 15+ minutes of extraction had already completed.
- The fix skips stale objects silently and logs a count, so taint annotations are missing for those nodes but the graph write still completes.

**Code added (inside the node loop)**
```python
stale_function_nodes = 0
# ...
if not (hasattr(n, "taint_categories") and hasattr(n, "taint_source") and hasattr(n, "taint_sites")):
    stale_function_nodes += 1
    continue
# ...
if stale_function_nodes:
    _log.warning(
        "[graph_ingest][repo=%s] taint marking skipped for %s function node(s) "
        "loaded without taint slots (stale extract/checkpoint cache). "
        "Clear caches before re-run: rm -rf .cache/graph_extract_cache .graph_checkpoints",
        repo,
        stale_function_nodes,
    )
```

---

## 3. `setup_cython.py`

**What changed**
No source change in this session. The file was already auto-discovering all eligible Python modules under `graph_core`, `ingest`, `instrumentation`, `sail_core` via `Path.rglob("*.py")`.

**Context**
During this session the compiled `.c` and `.so` artifacts were deleted (to clear a stale `models.so` mismatch) and the build was re-run with:
```bash
python setup_cython.py build_ext --inplace --force
```
All 37 modules compiled and copied successfully (EXIT_CODE=0).

---

## Safe cleanup command going forward

To delete only project-generated artifacts without touching venv binaries:
```bash
find . -type d -name venv -prune -o -type d -name .git -prune -o \
  -type f \( -name '*.so' -o -name '*.pyd' \) -print -delete
rm -rf build/
```

**Never** run a global `.so` delete without the `-name venv -prune` guard — it will break psutil, numpy, pandas, pyarrow, tree-sitter and every other native-wheel package in one command.

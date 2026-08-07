# Graph engine — current state, measured

Written 2026-08-07. Replaces `HANDOFF.md`, `IMPLEMENTATION_PLAN.md`,
`WORK_DONE_AND_NEXT_PHASES.md`, `CHANGES.md` and the generated `ACCURACY_AUDIT.md`,
all of which described a state the code has since moved past. `README.md` (how to
run the app) is still current and is not superseded.

Everything below is measured on the real ~43k-file Aramex repo unless it says
otherwise. Where something is unproven, it says so — the previous docs' habit of
recording predictions in the same voice as measurements is what made them
misleading once the code moved.

---

## 1. What works, with numbers

| | value | run |
|---|---:|---|
| files / nodes / edges | 43,289 / 759,999 / 2,976,752 | `bddb1204` |
| validation | **`ok: True`, no errors** | `bddb1204` |
| bytecode file coverage | **99.98%** (16,673 / 16,677 Java files) | `bddb1204` |
| `CALLS_EXTERNAL` | 64,725 (was 45,493 pre-catalog) | `bddb1204` |
| peak memory | 5,448 MB (was 9,140 MB at baseline) | `bddb1204` |
| taint sources marked | 6,671 functions | `bddb1204` |
| taint sinks marked | 6,479 functions | `bddb1204` |
| reachability universe | **42,313 of 354,142 (11.9%)** | reach |
| reach runtime | ~14 s | reach |
| path enumeration | 80 s, real multi-hop chains | paths |

Java `CALLS` are compiler-exact for 99.98% of files: read out of `.class` files,
so they are what javac resolved rather than a name match. That is the single most
important property here and everything downstream depends on it.

### The pipeline, end to end

```
354,142 functions
   ↓  catalog marks sources/sinks at INGEST (bytecode + heuristic paths)
  6,671 sources / 6,479 sinks
   ↓  reach.mark_all() — backward from sinks, forward from sources, to fixpoint
 42,313 universe (functions on some source→sink route)
   ↓  paths.sink_paths() — trusted edges only, hubs excluded, deduped
  N chains with per-hop provenance
   ↓  LLM judges exploitability
  findings
```

---

## 2. The vulnerability catalog

`graph_core/catalog/` — **128 owners, 413 signatures**: 13 sources, 71 sinks,
18 sanitizers, across 23 CWE categories. Plus **21 propagator owners / 86 methods**.

- **Mined** from FindSecBugs (`scripts/mine_findsecbugs.py`, re-runnable; the
  generated table is committed so builds need no network). LGPL-3.0 upstream;
  what is extracted is API signatures and their taint role, not detector code.
- **Curated** for what mining cannot supply: all 18 sanitizers (FindSecBugs
  encodes those in detector logic, so zero were mined), second-order sources
  (`ResultSet.getString`), weak randomness, insecure cookies, type coercion.

Three conventions that are load-bearing:

**Owner type first, never the method alone.** A table keyed on `close` tags
`inputStream.close()` as a database release; keyed on `write`, every logger
becomes an XSS sink. Most of `test_vuln_catalog.py` asserts these collisions do
NOT match.

**Argument positions are part of the entry.** `setString(int index, String value)`
sanitizes position **1**. An entry claiming position 0 would mark the placeholder
number as sanitized and the actual data as unsanitized — exactly backwards.

**Upstream indices are JVM stack SLOT offsets, not argument numbers.** Reversed,
receiver-addressable, and `long`/`double` occupy two each. All three were
established from specific upstream lines, not assumed — see
`scripts/mine_findsecbugs.py`. Getting this wrong inverts every sink.

### Type coercion is the highest-yield sanitizer class

`Integer.parseInt(request.getParameter("id"))` is *provably* safe — an int cannot
carry a payload. No upstream rule set models this (FindSecBugs' type system never
taints a primitive, so there is nothing to say). In a legacy app most numeric
parameters go through these, so it removes a large false-positive class for a
dozen entries.

---

## 3. Ingest-time marking

Set on `Function` nodes during the bytecode pass, before the pre-resolve write:

| property | meaning |
|---|---|
| `taint_source` | pulls in untrusted data |
| `taint_categories` | CWE classes this function reaches **directly** |
| `taint_sanitizer` | applies a catalogued sanitizer |
| `taint_sites` | JSON: `(line, category, role, arg positions)` per call site |

Named `taint_categories`, **not** `sink_kinds`, and the distinction is
load-bearing: `reach.py` already writes `f.sink_kinds` at analysis time using the
External-kind vocabulary (`db_execute`/`exec`/`file_write`). Two vocabularies in
one property would have collided silently, and reach.py runs later, so it would
have overwritten every ingest value with no error anywhere.

Both the bytecode path and the heuristic path mark. The heuristic one is not an
edge case: **a `.jsp` is compiled by the container at runtime and never has a
class file**, so it is the only marking JSP ever gets. In one measured run 18 of
20 `CALLS_EXTERNAL` edges came from `.jsp` files.

---

## 4. Reachability

`analysis/reach.py`, run by `reach.mark_all()` — which executes during a
**review**, not during a build. A freshly built graph therefore has
`taint_source`/`taint_categories` but no `from_entry`/`reaches_sink`, and reports
a universe of 0. That is two different sets of marks, not a broken build.

Measured seed contributions:

```
from_entry:   annotated_seeds      98      ← the pre-existing annotation path
              jsp_seeds         3,940
              catalog_entry_seeds 6,661    ← taint_source
reaches_sink: graph_seeds      12,192      ← CALLS_EXTERNAL edges
              catalog_seeds     2,519      ← taint_categories
              summary_seeds         0      ← no Pass A summaries yet
```

**98.** That is what entry-point detection found before the catalog seed existed:
98 functions out of 354,142. A legacy servlet app's entry points are
`service()`/`doGet()` — no annotation, no resolved route, not a JSP — so all three
structural seeds returned essentially nothing. Without `taint_source` seeding,
taint analysis on this codebase would have looked broken rather than unseeded.

### The universe is connectivity-bound, not seed-bound

Dropping `db_other` cut sink seeds 35% (12,192 → 7,942) and moved the universe by
**127 functions** (42,313 → 42,186, 0.3%). Backward closure from ~8k seeds reaches
83k functions in 12 iterations regardless of where it starts. So tuning
`DANGEROUS_KINDS` is not the lever; `_TRUSTED` (which edge strategies traversal may
follow) is.

---

## 5. Path enumeration

`analysis/paths.py`. Produces real chains with per-hop provenance:

```
[1.00] [db_execute,db_other] (3h)
  AmsMenuParam#canCreateInv -> AmsMenuParam#getMenuParam
    -> MenuParam#getMenuParam -> MenuParam#getMenuParamDetails
  sink=PreparedStatement.executeQuery,ResultSet.next   weakest hop: bytecode
```

Scored on the **minimum** hop trust — an average would let one fabricated hop hide
behind nine solid ones. On a measured run, 1,925 of 2,000 scored ≥0.8, all
all-bytecode.

### Truncation turns precision problems into recall problems

Results are `ORDER BY hops` and then truncated. With the old 2,000 limit, 359
`Class.forName` paths and 183 `ResultSet.next` paths filled the budget and real
`executeQuery` paths **were never returned at all**. This is why the limit is now
20,000: the cap has to sit above the real path count rather than act as a sampler.

Same mechanism bit twice more:
- All 2,000 returned paths were **0-hop** (entry and sink the same function — a
  JSP calling `executeQuery` in its own body). Filtering them out *afterwards*
  left zero, because the multi-hop ones were never fetched. `min_depth` is now
  part of the Cypher **pattern**.
- With `sink_kinds` unset, the sink end only had to have *some* `CALLS_EXTERNAL`
  edge, so `Integer.parseInt` (a catalogued sanitizer) and `Connection.close`
  ranked as top sinks. Now defaults to `reach.DANGEROUS_KINDS`, so both ends of
  the analysis agree on what "dangerous" means.

---

## 6. How to run

```bash
# environment
python3.12 -m venv venv && source venv/bin/activate
pip install --no-cache-dir -r requirements.txt
python setup_cython.py build_ext --inplace          # no --force needed; see §8

# verify before spending 30 minutes on a build
python -c "
import graph_core.resolver as r, graph_core.models as m
print('compiled:', r.__file__.endswith(('.so','.pyd')))
print('taint fields:', 'taint_categories' in m.Node.__slots__)"

# build (clear caches if Node's fields changed since the last run — see §8)
rm -rf .cache/graph_extract_cache .graph_checkpoints

# analysis, no LLM
python scripts/run_reachability.py --repo experiment
python scripts/show_paths.py --repo experiment --min-hops 1 --show 15
python scripts/show_paths.py --repo experiment --min-hops 1 --kinds db_execute
```

`GRAPH_CATALOG_EXTERNAL` defaults to `recommended` (every catalogued category
except format-string and log-injection, which are high-volume and low-yield).
`off` restores pre-catalog behaviour for a strict before/after.

---

## 7. What is NOT proven

Two claims with very different confidence, and conflating them is the main risk:

- **"This call chain exists"** — near-certain. Every hop is a `bytecode` edge at
  99.98% coverage.
- **"Untrusted data flows along it"** — unproven. There is no dataflow engine. The
  chain says a source is in the entry function and a sink at the end; it does not
  say the value reaches the sink. That is the LLM's job.

**No precision or recall number exists for this system.** Every figure in this
document describes what the tables contain or what the graph holds, never how
accurate a finding is. `scripts/score_owasp_benchmark.py` exists to produce that
number against OWASP Benchmark's 2,740 labelled cases; it has never been run.
Reading Benchmark's ground truth already exposed one gap before running it —
`weakrand` is 493 of its cases and the catalog had nothing, which is now fixed.

Known recall limits, all visible and tunable: `max_depth = 8` misses longer
chains; 121 hubs are excluded (a path whose only route is through `nullCheck` is
dropped, deliberately); `_TRUSTED` excludes name-strategy edges (~5% precision,
correctly excluded, but it is 19% of CALLS and mostly JS/JSP).

---

## 8. Traps that have already cost real time

**Never delete `.so` files without pruning `venv`.** This broke psutil, numpy,
pandas, pyarrow and tree-sitter in one command and cost a venv rebuild:

```bash
# WRONG — deletes compiled extensions inside site-packages
find . -name "*.so" -delete

# RIGHT
find . -type d -name venv -prune -o -type d -name .git -prune -o \
  -type f \( -name '*.so' -o -name '*.pyd' \) -print -delete
rm -rf build/
```

**Never commit Cython `.c` output.** Git sets checkout mtimes to *now*, so a
committed `.c` always looks newer than the `.py` it came from; `cythonize` treats
it as current, skips regeneration, and setuptools compiles the **old** code into
the new `.so`. That produced `'Node' object has no attribute 'taint_categories'`
on a tree that visibly had the field, and is why `--force` appeared to be needed.
37 `.c` and 186 `.pyc` files (29.6 MB) were untracked and ignored; regeneration is
now unconditional.

**Clear the extract cache when `Node` gains a field.** Pickled `Node` objects from
before the field exists are deserialized on resume, and `Node` is a `slots`
dataclass, so assigning the new field raises — 15 minutes into a run, after
extraction is paid for. `_apply_taint_marks` now skips such nodes with a warning
naming both causes rather than aborting.

**Pin `tree-sitter` and grammars exactly.** `>=` ranges let pip install a newer
grammar whose native binary is incompatible, producing
`No module named 'tree_sitter._binding'` on every import. `Cython` must also be an
explicit requirement — `setup_cython.py` needs it and fresh venvs failed without
it.

**Three producers append nodes after `merge_bundles` deduped them**
(`bytecode_resolver`, `resolver.resolve`, `_build_package_tree`), each with a
private id set. `external_id()` is a pure function of the API, so bytecode and the
resolver mint identical ids for any API both reach. All three now route through
one `_extend_unique` guard, first-wins — tree-sitter owns source positions, so a
synthesized stand-in must never displace a real extracted node.

---

## 9. Next

1. **Run `scripts/show_paths.py --kinds db_execute`** with the raised limit. If it
   comes in under 20,000 that is the complete, untruncated SQL-injection path set.
2. **Feed paths to the LLM.** The input is as good as this pipeline can currently
   produce: trusted edges only, hubs excluded, deduped, confidence-scored.
3. **Collapse same-tail duplicates.** `dedupe_paths` drops prefix-suffix sub-paths
   but not "same tail, different head" — one measured run returned 15 variants of
   `AmsMenuParam#* → getMenuParam → MenuParam#getMenuParam → getMenuParamDetails`.
   That is one finding, not fifteen.
4. **OWASP Benchmark**, for the first real precision/recall number.
5. **JspC precompile.** JSP calls JDBC directly here with no service layer, and
   `org.apache.jasper.JspC` would move it from the heuristic tier to the exact
   bytecode tier. Config, not code — highest-value remaining accuracy item.
6. **Deferred deliberately**: CFG/def-use dataflow. It buys determinism and lower
   LLM cost, not capability, and with a thin catalog it would have *worse* recall
   than the model. The 209 mined propagator rules are what it would need and they
   are already in the catalog, unused.

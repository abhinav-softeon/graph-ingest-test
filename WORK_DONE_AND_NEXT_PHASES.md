# Graph build — what was implemented, and what comes next

Written 2026-08-06. Companion to `HANDOFF.md` (measured baseline, known bugs) and
`IMPLEMENTATION_PLAN.md` (the original bytecode/JSP/JS plan).

**Everything below was implemented in `graph_build_test` only.** The vendored copy
at `sail/services/developer_assistant/.../graph_core/` was deliberately NOT touched.

> **Nothing here has been measured yet.** No full run completed against the real
> repo after these changes. Every expectation below is mechanism-based reasoning,
> and this project's own record (HANDOFF's methodological note) is that the
> mechanisms held while the magnitudes were wrong at least once. Treat the numbers
> as directions, not predictions.

---

## 1. The baseline everything is measured against

From `runs/b29bc9a9.json`, real ~16.5k-file Java repo:

| stage | seconds | share | stage peak RSS |
|---|---:|---:|---:|
| unzipping | 4.9 | 0.5% | 1,475 MB |
| discovering | 0.0 | — | 1,765 MB |
| graph_parsing (extraction) | 83.1 | 9% | 3,768 MB |
| **resolving** | **380.9** | **40%** | 5,561 MB |
| **deriving** | **419.5** | **44%** | **7,424 MB** |
| writing_graph | 56.3 | 6% | 5,030 MB |
| **total** | **944.7** | | **overall peak 9,140 MB** |

Two facts that drove every decision:

1. **84% of wall time is single-threaded** (resolve + derive). Extraction is the
   only parallel stage, so more cores can never buy more than ~9%.
2. **Derive is the most expensive stage AND the memory peak**, and it was not in
   the original plan at all.

Accuracy baseline (HANDOFF §3.2, javac oracle vs graph):

| | precision | recall |
|---|---:|---:|
| ALL CALLS | 5.0% | 93.8% |
| confident (non-AMBIGUOUS) | 38.4% | 92.9% |
| AMBIGUOUS only | **0.1%** | 1.0% |

And the single largest known defect: **8,236,060 of 8.38M REFERENCES** carried
`name+arity+unknown_recv` — "receiver type known, method not found on it" — which
in an `Abstract*`/`I*`-heavy codebase is overwhelmingly *inheritance*, not a real
miss.

---

## 2. Implemented — Phase 1 (accuracy)

### 2.1 `EXTENDS` / `IMPLEMENTS` from the class header

`bytecode_resolver.resolve_java_bytecode`. Read straight from JVMS §4.1
`super_class` / `interfaces`, emitted at `Confidence.EXTRACTED`,
`strategy="bytecode"` — compiler facts, not the name/scope/import guesses
`resolver.py` makes for the same edge types.

Collected during the **existing** single parse pass (`hierarchy`, `own_class_ids`,
`declared_methods` dicts), so no extra traversal of the class files.

Two deliberate decisions:

- **Only a class's OWN node is used** (`own_id`, not `owner_class_id`). The latter
  falls back to the *enclosing* class for anonymous classes, so using it would
  claim `Outer` extends whatever a callback implements.
- **Emitted additively** — the heuristic still resolves these ref types for the
  same files. Bytecode looks the supertype up by full binary name via fqn, while
  the heuristic matches simple name + imports, so on any class whose extracted
  fqn is spelled differently from the binary name the heuristic can still find a
  target bytecode misses. Suppressing would trade a known-small duplicate count
  (HANDOFF measured only 8,320 EXTENDS + 1,138 IMPLEMENTS) for an unknown number
  of lost edges. Neo4j MERGE collapses duplicates on `(type, src, dst)` anyway.
  **Revisit once the overlap is measured** — see §5.

### 2.2 Descriptor-exact `OVERRIDES`

New `NodeIndex.override_target()` in `bytecode/matcher.py`, plus
`can_override()` and `MethodInfo.is_private`.

The problem: `pipeline._derive_overrides` matches on name + **`param_count`**,
because `param_types` is dropped by the slim node projection long before derive
runs. So it cannot tell `foo(String)` from `foo(int)` — both are (name, arity 1).
A false `OVERRIDES` does not stay contained: polymorphic dispatch fans out over
it, and consumers join through it at query time.

`override_target()` is deliberately **stricter than `method_id()`**: it requires
exact erased *parameter-type* equality and never accepts an arity-only match.
Return type is ignored on purpose, because Java allows a covariant return
(`Object clone()` → `Foo clone()`) and javac records that with a bridge method
rather than by changing the override relationship.

`can_override()` excludes static (hides, not overrides), private (invisible to
subclasses), constructors, `<clinit>`, and lambda bodies.

**Authority handoff.** `_derive_overrides` gained `skip_methods` / `skip_pairs`:

- `skip_methods` = `BytecodeReport.authoritative_override_methods` — methods whose
  **entire** ancestor chain was walked without hitting an unparsed in-repo class.
  For those, bytecode's answer is complete *including the negative answer
  "overrides nothing"*, so re-deriving could only add false pairs.
- `skip_pairs` = pairs already emitted, so a truncated-chain method cannot get the
  same edge written twice at two confidences (MERGE last-write-wins would
  downgrade `EXTRACTED` → `INFERRED`).
- Methods absent from the set keep the heuristic, so partial bytecode coverage
  degrades gracefully. `override_chains_truncated` counts the gap.

Both sets are **cleared immediately after derive consumes them** — one entry per
override-capable method (hundreds of thousands of ids) held across the derive
memory peak, inside an object returned in `IndexResult`, for nothing.

### 2.3 Inheritance-aware call resolution

`resolver.py` — the largest accuracy item, aimed directly at the 8.2M bucket.

`methods_of_class[(class_id, name)]` holds a class's **own** methods only, so a
call to an inherited method found nothing, fell through to the arity-only
fallback, fanned out across every same-named method in the repo, and was then
demoted to a weak `REFERENCES` edge tagged `+unknown_recv`.

Added an **ancestor pre-pass**, seeded from two sources:

1. `EXTENDS`/`IMPLEMENTS` **edges** a precise tier already produced (bytecode, per
   §2.1) — compiler facts where coverage exists.
2. Leftover hierarchy **refs**, resolved via `narrow_type` with the same
   import/package-aware narrowing they'd get in the main loop. **Unique matches
   only** — a wrong supertype redirects every inherited call beneath it.

It **must** be a pre-pass: those refs are otherwise resolved inside the main loop,
so consulting a hierarchy built as it goes would make a call's answer depend on
whether its ancestor happened to resolve first — results varying with input order.

The hierarchy refs are collected inside the **existing** ref scan (`hierarchy_refs`)
rather than a second pass over ~5M refs.

Then all three receiver steps in `narrow_call` fall back to `_inherited_members()`
on a miss, tagged `receiver_type*_inherited`. Two properties matter:

- **Own methods are tried first**, so an override correctly shadows its ancestor.
  Reversing that order would silently send every overridden call to the wrong
  declaration.
- `_ancestor_chain` is **breadth-first and memoized**, so the nearest declaration
  wins (matching Java dispatch) and a deep hierarchy is walked once per class, not
  once per call site. `seen` also makes it cycle-safe on a malformed hierarchy.

Because the strategy no longer starts with `name`, these no longer hit the
`unknown_recv` demotion — which is what turns them back into real `CALLS`.

---

## 3. Implemented — Phase 2 (memory and speed)

### 3.1 In-place slim conversion — the derive memory peak

`pipeline.py` previously did:

```python
all_edges = [e.to_slim() if isinstance(e, Edge) else e for e in all_edges]
```

That built a **second ~15M-element list while the first was still alive**, and
kept every full `Edge` alive until the comprehension finished — so the stage's
peak held *both* representations of the entire edge set at once. That is the
5,561 MB → 7,424 MB jump.

Now converts in place, so each `Edge`'s refcount hits zero as soon as its
`SlimEdge` replaces it: the two representations overlap by one element instead of
15 million. `del pending` first, because that write list held a reference to every
full `Edge` and would have defeated the incremental release.

### 3.2 One edge scan in derive instead of two

`shared_parent_of` (CONTAINS) and `shared_supers` (EXTENDS/IMPLEMENTS) now build
in a single traversal; `_derive_overrides` accepts `supers` instead of re-scanning
all ~15M edges. Verified output-identical by test.

### 3.3 Ambiguity cap on the `unknown_recv` path

**The real find.** The cap already existed (`GRAPH_NAME_MATCH_MAX_CANDIDATES`,
default 5) but lived in `emit()` — and the demotion branch appends its edges
**directly, bypassing `emit()`**. So the single largest fan-out path in the whole
resolver had no cap at all.

Now reuses the same dial and the same `name*` scoping, so one setting governs both
paths and the trusted tiers (`same_scope`/`same_file`/`imports`/`receiver_type`)
stay uncapped. Saves memory three times over — resolve, derive's whole-graph
passes, and the write.

**Cost:** ~0.9pt recall on the capped tier, per HANDOFF's measurement (dropping
ambiguous costs 0.9pt recall for 7.7× precision and 87% fewer edges).

### 3.4 Not done, deliberately

`_derive_sql_links` early-returns when there are no `Table` nodes (there are none
— `.sql` is stripped at upload). Its only cost is one node scan, ~30 ms. Not the
419 s culprit; not worth the risk.

---

## 4. Implemented — observability

Previously `resolve()` **returned** `coverage` and logged none of it, and the ~10
`graph_parsing` progress calls fired the UI callback while logging nothing — so a
terminal tailing a 16.5k-file extraction went silent for minutes.

- **Per-ref-type coverage** logged at end of resolve (`total/resolved/ambiguous/
  unresolved/external` + % pinned).
- **`edges by strategy`** — HANDOFF lists this as a tracked metric; nothing
  produced it. One dict increment per edge in `make_edge`.
- **Cap accounting**: `ambiguity cap dropped N bare-name site(s) and N
  unknown-receiver site(s)` — both caps were silently discarding work.
- **`inheritance-aware resolution produced N edge(s)`** — the payoff of §2.3, as
  distinct from the size of the index it built.
- **`config in effect:`** — one line stating what is *actually* engaged, because
  both big levers fail silently: `GRAPH_STREAMING_INGEST` does nothing without
  `GRAPH_CHECKPOINT_ROOT`, and the compiled hot path is a build-time step so an
  image built without it runs interpreted with no error anywhere.
- **Formatted progress**, once, for both outputs: `graph_parsing | 3,412/16,500
  (20.7%) | path/File.java [java] (peak_rss=3768MB)`. Done by rebinding
  `on_stage` to a wrapper rather than editing 17 call sites. Per-file progress is
  rate-limited to 2 s; milestones are never throttled. `_peak_rss_mb()` is
  sampled at most 1×/s (it builds a fresh `psutil.Process()` and reads `/proc`).
- **`instrumentation/log_stream.py`** — bounded ring buffer (4,000 lines) behind a
  root `logging.Handler`, rendered as a scrolling panel in the UI **including on
  failure**, which is when it matters most (an OOM kill produces no run report at
  all). Thread-safe by construction (`deque.append` is atomic), long lines
  truncated, noisy third-party loggers muted, strictly additive — never touches
  the stdout handler `docker compose logs` reads, and `install()` refuses to
  *lower* the root log level.
- **Build handle at module scope** (`_ACTIVE_BUILD` in `ui/app.py`) — it lived
  only in `st.session_state`, which is per browser session, so a page refresh lost
  the build entirely (no timer, no panel, no STOP) while the thread kept running.
  A module global survives session loss because Streamlit re-executes the script
  but keeps the module imported.

---

## 5. Open items — nothing here is done

| # | Item | Why it matters |
|---|---|---|
| 1 | **Nothing is measured.** No full run has completed post-change. | Everything above is unverified. This is the top priority. |
| 2 | **Cancellation only works during `resolving`** — `cancel_check` is honored in exactly one place (`resolver.py`, main ref loop, every 50k refs or 3 s). Extraction, derive, write and unzip ignore it entirely. | STOP during extraction does nothing for minutes. Work was started and interrupted; not implemented. |
| 3 | **`narrow_call` step ordering**: same-file (step 2) beats receiver-type (step 4). Proven to return `AMBIGUOUS` across two unrelated classes where receiver-type would pin exactly one. | Real precision bug. Riskier than anything above — needs its own measurement. |
| 4 | **`javac_autocompile.py` has no `-classpath` flag at all.** | It fails on any file importing a third-party class, i.e. nearly everything in an enterprise webapp. Raising bytecode coverage feeds §2.1/2.2/2.3 directly. Highest-value remaining Java item. |
| 5 | **Decide on `EXTENDS`/`IMPLEMENTS` suppression** (§2.1) once overlap is visible. | Small win; currently additive on purpose. |
| 6 | **Bytecode does not extract class-hierarchy `OVERRIDES` for non-parsed ancestors**, so `override_chains_truncated` is a real residual. | Bounded by bytecode file coverage. |
| 7 | **Build dies with the process.** Tab reload survives; `docker compose restart` / OOM does not. Proper fix = move the build out of the Streamlit process (`api.py` already exists as a FastAPI entrypoint with background tasks). | Only matters for long runs. |
| 8 | **Docs stale**: `HANDOFF.md` still lists the inheritance gap (P3) and ambiguous cap (P7) as *planned*; `IMPLEMENTATION_PLAN.md` Phase 2 items are done. | Will mislead the next session. |

---

## 6. Next phases

### Phase 3 — the vulnerability catalog (no dependencies, can start now)

A flat lookup table, external to any code parsing:
`fully-qualified API signature → role (source / sink / sanitizer)`. **Not**
produced by tree-sitter or any parser — imported once as reference data, then
consulted when a call is identified. It says nothing about your own code.

Bootstrap from existing open-source rule sets rather than building from scratch:

**Java**
- **FindSecBugs** — `github.com/find-sec-bugs/find-sec-bugs` (LGPL). Best
  Java-specific starting point; bytecode-level detectors with an internal taint
  config that is effectively a plaintext source/sink list.
- **CodeQL Java security library** — `github/codeql`,
  `java/ql/lib/semmle/code/java/security/` (MIT/Apache). The most rigorous option;
  per-CWE files listing exact API signatures. Readable as source — you're
  extracting a taxonomy, not running their engine.
- **Semgrep registry** — `semgrep/semgrep-rules`, `java/`. Broader framework
  coverage (Spring especially), less curated per entry.
- **PMD** — `category/java/security.xml` (BSD). Smaller, still worth including.
- **OWASP Benchmark** — `OWASP-Benchmark/BenchmarkJava`. Not a catalog: a large
  labeled test suite. Useful for mining real API usage *and* for validating
  whatever catalog you build.

**Python**
- **Bandit** — `PyCQA/bandit` (Apache 2.0). Best Python starting point; plugin
  source enumerates the dangerous calls directly (`subprocess` with `shell=True`,
  `yaml.load` without `SafeLoader`, `eval`/`exec`, weak crypto, SQL string
  formatting). Plain readable Python, the most directly minable of the lot.
- **CodeQL Python security library** — same repo, `python/ql/lib/semmle/python/
  security/`. Flask/Django sources and sinks already curated.
- **Semgrep** — `python/` directory, Django/Flask/SQLAlchemy tagged.
- **PyT** — `python-security/pyt`. Dated, but an academic Python-specific taint
  analyzer with hardcoded Flask/Django source/sink lists worth mining for shape.

**Caveat:** none of these hands you a clean CSV. FindSecBugs and CodeQL entries
are embedded in detector/query source, so flattening them into a lookup table is
itself a small extraction task (light scripting, not hard). Bandit's are the most
directly readable.

Backend-only scope: JS/TS and SQL catalog work is deliberately out.

### Phase 4 — CFG + DFG + taint (consumes Phase 3)

Classical, deterministic compiler theory — **no LLM in this piece at all**. Same
category Fortify/Checkmarx/Coverity use.

1. **CFG** — split each function body into basic blocks, connect via
   branch/loop/try-catch edges, directly off the resolved AST. Per-function and
   local, so it is embarrassingly parallel like extraction and does **not** need
   to wait on `resolve()`.
2. **DFG** — reaching-definitions (Kildall's algorithm) over the CFG, propagating
   def-use chains to a fixpoint. Same *idea* as `reach.py`'s whole-repo fixpoint,
   at statement level inside one function.
3. **Taint propagation** — seed "tainted" at catalog-recognized sources, propagate
   along def-use edges, flag on reaching a catalog-recognized sink without passing
   a catalog-recognized sanitizer.
4. **Interprocedural joining** — compute each function's summary **once**,
   context-insensitively and parameter-relative ("if param 0 is tainted it flows
   to arg 1 of X"), then apply that same summary at every call site using *that
   site's* bound argument. This is what answers "one function, many call sites,
   different data each time": the differentiation lives on the **edge**, not in
   duplicated nodes. It is exactly the shape the existing Pass A (generic
   per-function summary) → Pass B (path-specific join) pipeline already has.

**Storage precedent exists**: the deleted `dataflow.py` emitted `PASSES` edges
(param/field flows into call args) plus a `dfg_json` blob per Function node.
Nothing new to invent there.

**⚠️ Known prior failure — must not repeat.** The original `dataflow.py` directly
contributed to real OOM crashes on a ~20k-file ingest (`RECENT_CHANGES.md`): it
held `DataflowResult`/`dfg_json` for the whole repo, duplicated, and used **full
(non-slim) `Node` objects in its own lookup indices** — an explicitly flagged,
still-unaddressed risk at removal time. Any revival must use the memory patterns
the pipeline has since adopted: slim node projections, spill-to-disk-and-drop.
Note §3.1 above is the same class of bug, found again in a different place.

**Design nuance that decides whether this is worth building:** a deterministic DFG
has *zero* general knowledge — it only recognizes what is explicitly in the
catalog. A DFG with a small catalog would have **worse recall** than a pure-LLM
approach. So the correct design is **hybrid, not replacement**: DFG as ground
truth wherever the catalog matches (deterministic, Fortify-grade precision there),
LLM as fallback everywhere else. Never worse than today, and it converges toward
parity as the catalog grows. **Catalog breadth is the dial**, not the algorithm.

**Cost honesty:** CFG/DFG is *added* work. It makes graph-build time go **up**, not
down. The speed case is indirect — if a later pass can trust a precomputed
deterministic fact instead of paying for an LLM call, cost shifts from expensive
to cheap. That is a redistribution, and only a net win if the downstream saving
exceeds the construction cost.

### Phase 5 — Python

- `scip-python` (Pyright-backed) was **deleted** from this sandbox along with
  scip-java (HANDOFF §2.5), because scip-java could never run — the target repo
  has no build files. Re-adding scip-python is therefore *new work*, not a toggle.
- Needs no compile step, unlike Java. **Network is needed only at one-time setup**
  (installing scip-python/Pyright, and optionally the repo's deps for cross-package
  types). The indexing run itself is pure static analysis over local files — zero
  network. So bake it into the image at build time.
- Structure it as its own resolver with its own `attributed_files`, mirroring
  `bytecode_resolver.py`, then feed that into the existing `skip_call_files`
  exclusion **before** `resolve()` runs — that is the actual "reduce the amount of
  resolve" part.
- Separate catalog from Java's (Flask/Django/SQLAlchemy APIs).
- **Structural ceiling, not just an engineering gap**: Python's dynamic typing
  (duck typing, monkey-patching, `getattr`) means even perfect tooling has a lower
  resolution ceiling than Java's static type system. "Same accuracy as Java" is
  not fully achievable here regardless of effort.

### Deferred

- **JS/TS** — `scip-typescript` exists (Sourcegraph, real TS compiler API) but is
  not integrated at all. Plain JS with no annotations has an even lower ceiling
  than Python.
- **SQL** — no direct SCIP equivalent (schema matching, not compiled-symbol
  resolution). For injection detection specifically a SQL parser is **not** the
  lever: you need catalog-based sink classification plus a dataflow question about
  the *calling code's* argument. A real parser (`JSqlParser`/`sqlglot`) plus schema
  validation only matters for the secondary table/column-linking use case.

---

## 7. Running it

### macOS (recommended — 24 GB, no VM, no filesystem bridge)

```bash
xcode-select --install                      # C compiler, for the Cython step
brew install python@3.13                    # if not present
cd graph_build_test
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python setup_cython.py build_ext --inplace  # THE big win: 84% of wall time
export GRAPH_LOG_LEVEL=INFO
python -m streamlit run ui/app.py
```

Also needs **Neo4j** reachable (Desktop or a container publishing 7687), with
`.env` pointing at `bolt://localhost:7687` — `host.docker.internal` was
container-only. A **JDK is not required**: `bytecode/classfile.py` is a pure-Python
class-file parser, so existing `.class` files work as-is. A JDK is only needed for
`javac_autocompile` / `javac_resolver`.

Confirm the first log line reads `compiled: resolver=True pipeline=True`.

At 24 GB the memory-specific work stops being critical — consider running with
checkpointing **off** for faster extraction (it pickles every batch bundle to
disk; that bought ~750 MB, which mattered at a 7 GiB cap and does not at 24 GB).

### What to read off the first real run

1. `config in effect:` — verify the levers engaged at all.
2. `deriving` `mem_peak_mb` vs **7,424 MB** — whether §3.1 worked.
3. `stage_seconds` resolve/derive vs **380.9 / 419.5** — Cython + §3.2.
4. `edges by strategy` — a large `receiver_type*_inherited` means §2.3 hit the
   8.2M bucket. A still-huge `name+arity+unknown_recv` means it did not.
5. `ancestor index: (N edge-sourced, ...)` — `0` means bytecode contributed no
   hierarchy and it all came from heuristic refs.
6. `ambiguity cap dropped N` — the recall cost, now visible instead of silent.
7. `match_rate` / `file_coverage` — how much of the Java the `.class` files cover.
   Everything in §2 scales with this.

---

## 8. Tests

231 passing. Three new files:

- `graph_core/tests/test_bytecode_hierarchy.py` (18) — EXTENDS/IMPLEMENTS/OVERRIDES.
  Mostly **negative** tests, since the point is precision: same-arity/different-type
  is not an override, statics hide, privates never override, covariant returns
  still count. Includes a **control test asserting the OLD behavior DOES emit the
  false pair**, so the suppression test cannot pass for the wrong reason.
- `graph_core/tests/test_inheritance_resolve.py` — the ancestor pre-pass. One class
  per file **deliberately**: with the whole hierarchy in one file, same-file
  narrowing (step 2) wins before receiver-type (step 4) is ever reached, and the
  code under test never runs.
- `graph_core/tests/test_unknown_recv_cap.py` (4) — the cap, with a control test
  that disabling it restores the full fan-out.

### Two fixture bugs the control tests caught

Both would have made assertions pass **vacuously**, and both are worth remembering
because they are easy to repeat:

1. Whole hierarchy in one file → resolved via `same_file+arity` as AMBIGUOUS,
   never reaching the receiver-type path.
2. Passing `resolve()`'s **output** alone as `edges` → no `CONTAINS` edges (those
   are extraction's, passed *in*), so `_derive_overrides` silently returned
   nothing and every "it was suppressed" assertion trivially held.

Guard against both with an explicit `assert <result>, "fixture produced nothing"`.

---

## 9. Honest record of mistakes made while doing this work

Kept because each one is a live trap for the next person.

1. **Emission before the guard.** The hierarchy/OVERRIDES emission initially ran
   *before* the stale-build guard, so a rejected bytecode pass would still populate
   `authoritative_override_methods` — telling `_derive_overrides` to stand down
   while supplying nothing, leaving those methods with **zero** overrides. Moved
   below both guards. Discarding a pass must discard its *claims* too.
2. **Handoff sets held across the memory peak** and returned inside `IndexResult`.
   Now cleared after derive consumes them.
3. **`_peak_rss_mb()` per progress event** — fine at ~23 `_beat` calls per run,
   wrong once the wrapper called it per file. Now sampled ≤1×/s.
4. **Blamed the wrong things for slow extraction.** Attributed it to core count
   and VM memory; both turned out to already be correct (12 CPUs, 9.71 GiB live).
   The cause is still unidentified — do not repeat those explanations, measure.
5. **Called a cosmetic issue a defect.** Claimed the `·` separator caused mojibake;
   the container's stdout is UTF-8 and it was only a local Windows cp1252 terminal.
6. **Set the container cap too close to the VM size** — 7 GiB inside a 7.76 GiB VM
   leaves ~760 MB for kernel + dockerd + *all page cache*, which is its own
   performance problem.

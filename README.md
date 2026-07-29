# graph_build_test

Builds a code knowledge graph from a source zip into Neo4j. It is the ingestion
half of the code-review / security-analysis service in `sail/services/developer_assistant`
— this repo is the sandbox copy of `.../graph_engine/graph_core`.

Deterministic: tree-sitter parsing plus a heuristic resolver. No LLM anywhere in
the build.

---

## How a graph gets built

Six stages, in order. Every stage logs `peak_rss=NNNmB`.

**1. `discovering`** — enumerate candidate file paths. No file reads.

**2. `graph_parsing`** — the long CPU-bound phase. Files are processed in
batches (`GRAPH_EXTRACT_BATCH_SIZE`) across a process pool; each batch is
discovered, read, tree-sitter parsed, and merged. Raw source dies at the end of
each batch.

The important output distinction: extractors emit only `CONTAINS`, `DEFINES` and
`EXPOSES` as real edges. Everything else — calls, inheritance, imports,
annotations — comes out as a **`RawRef`**: "this symbol references the *name*
`foo`", destination unknown. With `GRAPH_CHECKPOINT_ROOT` set, each batch is
written to disk and dropped from RAM instead of accumulating.

**3. `resolving`** — two things happen.

First, extracted nodes are written to Neo4j and `all_nodes` is replaced in place
with a slim projection (16 fields, 360 B → 160 B per node). Neo4j is their source
of truth from that point.

Then `resolve()` builds ~12 lookup indices over those nodes and walks every ref,
turning names into edges. **This is where `CALLS`, `EXTENDS`, `IMPLEMENTS` and
`ANNOTATED_WITH` come into existence** — they do not exist before this point.
Resolution order: `self`/`cls` → enclosing class, same-file preference,
import-aware narrowing (including exact owner-FQN match), receiver-type
inference, then arity. Each ~10k-edge batch goes to a background writer thread.

**4. `scip_python` / `scip_java`** *(optional, off by default)* — replaces
heuristic `CALLS` for a whole language with type-precise ones.

**5. `deriving`** — `_derive_overrides` (OVERRIDES from the class hierarchy),
`_build_package_tree`, `_derive_sql_links` (READS/WRITES to Table nodes).

**6. `writing_graph`** — late nodes, the derived edges, then a Cypher pass that
creates the polymorphic-dispatch CALLS edges server-side, then validation.

### Graph model

```
nodes:  Annotation Class Endpoint Event Field File Function Module
        Package Policy Repository Table

edges:  ANNOTATED_WITH AUTOWIRED BELONGS_TO CALLS CALLS_API CATCHES
        CONSUMES_EVENT CONTAINS DEFINES EMITS_EVENT ENFORCES_POLICY
        EXPOSES EXTENDS HAS_GENERIC HAS_TYPE IMPLEMENTS IMPORTS
        INSTANTIATES OF_TYPE OVERRIDES READS REFERENCES REQUIRES_AUTH
        RETURNS RE_EXPORTS THROWS USES WRITES
```

Every edge carries `confidence` (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`) and
`origin` (`EXTRACTED` / `DERIVED`).

### What is held in RAM, and what is not

This is the core of the current design:

| | held | why |
|---|---|---|
| nodes | slim projection, whole run | every derive pass reads them |
| structural edges | `SlimEdge`, whole run | derive re-reads them arbitrarily; bounded by *declarations* (~2-3/node) |
| **the CALLS bulk** | **not held** | written to Neo4j by the resolve sink, then dropped |
| refs | streamed from disk when `GRAPH_STREAMING_INGEST` + checkpointing | otherwise resident |

Retained edge types are `_RETAINED_EDGE_TYPES` in `pipeline.py`. Measured on the
Python corpus: of 1946 edges produced by resolve, **18 are retained (0.9%)**.

---

## Running it

```bash
streamlit run ui/app.py          # UI — NOT `python ui/app.py`, that renders nothing
python cli.py --zip x.zip --project name
```

The UI sidebar has a **Profile** preset (Balanced / Low RAM / Fast) that sets the
interacting knobs together, because the right combination is not obvious: ref
streaming does nothing without checkpointing, and extraction RAM is
`workers × batch size`, not either alone.

### Settings that still change behaviour

`GRAPH_EXTRACT_BATCH_SIZE`, `GRAPH_EXTRACT_WORKERS`, `GRAPH_CHECKPOINT_ROOT`,
`GRAPH_STREAMING_INGEST`, `GRAPH_SCIP_ENABLED`, `GRAPH_EXTRACT_CACHE_ENABLED`,
`GRAPH_EXTRACT_CACHE_DIR`, `GRAPH_WRITE_WORKERS`, `GRAPH_WRITE_BATCH_SIZE`,
`GRAPH_CACHE_IO_WORKERS`, `GRAPH_ZIP_EXTRACT_WORKERS`,
`GRAPH_RESOLVE_CHECKPOINT_SECONDS`, `GRAPH_INDEX_LOCK_STALE_SECONDS`,
`SCIP_PYTHON_BIN`, `SCIP_JAVA_BIN`, `SCIP_JAVA_MAX_FILES`, `NEO4J_*`.

Node/edge early-write, edge slimming, the slim node projection, and dropping the
bulk edges are **unconditional** — there is no toggle.

---

## What changed (this rework)

Everything below was verified byte-identical on both test corpora unless stated:
edge-set hash, every edge key, every node id, and per-reftype resolver
`Coverage`.

### Bugs found

- **`GRAPH_LOWRAM_DERIVE` was unreachable.** Its guard rejected `stream_writer`,
  which had become an unconditional `True`. It had never run since that change.
- **Its guard fired after `store.wipe()` and the node write**, so a rejected run
  left the repo half-ingested in Neo4j.
- **It silently produced a wrong graph.** `EXTENDS`/`IMPLEMENTS` only exist after
  `resolve()`, but the code split edge types on the *pre-resolve* list, so the
  real ones went to disk and `_derive_overrides` saw an empty class hierarchy —
  **zero OVERRIDES, no error**. Proven 0 → 5 on the Java corpus.
- **Two always-dead validation checks**: `validate_graph` warned "function nodes
  missing core metrics" on *every function of every run* (it read `loc`/
  `cyclomatic`, which the pre-resolve write blanks), and its `end_col < start_col`
  check could never fire for the same reason.
- **`arg_names` was computed and discarded.** Both extractors walked every call
  site's argument list and never passed the result to `ref()`.
- **`GRAPH_RESOLVE_WORKERS` could not work.** Parallel resolve required
  `resolve_sink is None`, but the streaming writer sets a sink on every run.
- **`ingest/build.py` and `api.py` broke** on removed symbols mid-rework — caught
  by an import smoke test, not by the unit tests, which never import the app layer.

### Removed

| | why |
|---|---|
| `PASSES` edges + 4 payload arrays | zero consumers in the whole sail monorepo |
| the DFG pass + `dfg_json` | deliberate swap: Agent C reads source instead. Removed a **second full-repo tree-sitter parse** |
| `GRAPH_LOWRAM_DERIVE` + `lowram_derive.py` | its disk spill became write-only |
| `_synthesize_polymorphic_calls` (both variants) | moved into Neo4j as Cypher |
| `resolver_parallel.py`, `GRAPH_RESOLVE_WORKERS` | unreachable |
| `arg_names`, `GRAPH_RESOLVE_CHUNK`, `GRAPH_STREAMING_WRITER` | dead |
| joblib dump block, `IndexResult.roles`, `_derived_semantics_rows` | dead |
| 12 node properties + 3 edge properties from the Neo4j write | no query-side consumer |

**~2000 lines deleted across 62 files.**

### Memory, per unit

Marginal object cost, measured:

```
SlimEdge      88 B -> 56 B      (identity only)
Node         360 B -> 160 B     (slim projection)
node write    30   -> 18 props
edge write     8   -> 4 props
resolver nested indices  311 B -> ~40 B per entry
```

Projected at 1M nodes / 130M edges: the retained bulk edge list goes from ~8.3 GB
to ~0; nodes from ~0.54 GB to ~0.17 GB; `dfg_json` (~1.8 GB) and PASSES (~1.0 GB)
gone entirely.

**These are projections from measured object sizes, not observed RSS.**

---

## What to do next

### 1. Run it for real — nothing below matters until this happens

Every number above is arithmetic on object sizes measured against two corpora
totalling **41 files**. Specifically unverified:

- **The Cypher polymorphic pass has never executed.** Not once. The tests can
  only assert the query is *issued* — neither corpus has callers of an overridden
  method. This is the highest-risk change in the rework.
- **Peak RSS has never been measured** on a real corpus. The instrumentation
  already exists: `MemorySampler` tracks peak across child processes, and
  `RunReport` records `mem_peak_mb` per stage. The UI shows both. One run answers
  where the wall actually is.
- **Uncapped polymorphic dispatch** creates strictly more edges than the old
  top-25 cap on wide hierarchies. Worth watching on AuraDB write volume.

### 2. Port to `sail`

`sail/services/developer_assistant/app/services/code_review/graph_engine/graph_core/`
needs the same changes, and `taint.py`'s pass 1 must be retired with them — it
reads a `dfg_json` that no longer exists. Do not deploy one without the other.

Rebaseline `scripts/graph_fingerprint.py` at the same time; its node-property
hash was repointed at properties that still exist.

### 3. Then, if RAM is still the problem

In order of value:

- **`all_refs`** — ~150 B × millions, and it scales with *call sites*. Already
  solvable with `GRAPH_STREAMING_INGEST` + `GRAPH_CHECKPOINT_ROOT`; confirm it
  actually helps before building anything new.
- **Resolver indices** — `by_name` / `classes_by_name` / `endpoints_by_key` are
  still `defaultdict(list)` at 119 B/entry, where the single-element list is pure
  waste. The three *nested* ones were already flattened. Left alone deliberately:
  their read sites are spread through the matching logic, and a mistake there
  changes resolution rather than just memory.
- **Full nodes during extraction** — the slim projection only happens at the
  pre-resolve write. Writing and projecting per batch would bound it, but
  `merge_bundles` dedups across batches at the end, so the interaction needs
  thought.

### 4. Worth having regardless

- **A way to look at the graph.** There is none. `graphify` (`../graphify`)
  produces a `graph.html` and a report; this pipeline produces numbers. Given how
  much of this rework was validated against counts rather than output, that is a
  real gap.
- **Comment/docstring → code edges.** `graphify` models these as
  `rationale_for` and it has no equivalent here. For a review agent it is a
  direct feed of author intent.
- **Decide `arg_names`.** Currently removed. It cannot disambiguate overloads
  (Java overloads are already distinct nodes — their id includes the parameter
  signature — and `_apply_arity` narrows on arity). The one real use would be
  matching Python keyword arguments against `param_names`.

---

## Tests

```bash
python -m pytest graph_core/tests/ -q
```

Three files, and the guardrails matter more than the count:

- `test_slim_node.py` / `test_slim_edge.py` — **AST-scan** every consumer for
  reads of a field the slim projection drops. `all_nodes` is a `SlimNode` list
  from the pre-resolve write onward, so a field read no corpus happens to
  exercise would otherwise be an `AttributeError` waiting in production.
- `test_pipeline_contract.py` — OVERRIDES really being derived from the
  *resolved* hierarchy (the zero-OVERRIDES bug above); the bulk edges not being
  retained (a regression nothing else would notice — the graph would still be
  correct, just gigabytes heavier); removed payload fields staying removed.

**Known gaps:** nothing exercises the Cypher polymorphic pass, SCIP, or
incremental ingest. The unit tests do not import the app layer (`ui/`, `cli.py`,
`api.py`, `ingest/`), which is why two import-level breaks got through during the
rework — a smoke test that imports every module is worth adding.

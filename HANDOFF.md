# Graph engine — state, metrics, and planned work

Handoff for a fresh session. Written 2026-07-30. Self-contained: everything below
is either measured on the real 16.5k-file Java repo or verified in code, with
`file:line` references so nothing has to be re-derived.

Repo: `D:\abhinav\second_developer\graph_build_test` (sandbox)
Production copy: `sail/services/developer_assistant/app/services/code_review/graph_engine/graph_core/`
**The sandbox changes below are NOT yet ported to the production copy.**

---

## 1. What the pipeline does

Zip of source → tree-sitter extraction → heuristic name resolution → derive →
Neo4j. Six stages in `graph_core/pipeline.py:index_repo`: `discovering`,
`graph_parsing`, `javac` (new), `resolving`, `deriving`, `writing_graph`.

Nodes: `Repository Package File Module Class Function Field Endpoint Event
Policy Annotation Table`. Edges: see `graph_core/schema.py:27`.

---

## 2. Changes made this session

### 2.1 Java receiver typing — `graph_core/extractors/java.py`

**The root cause of ~62M junk edges.** `java.py` emitted CALLS refs with the
method name only — no `recv`, no `recv_type` — so `narrow_call`'s receiver-type
step (`resolver.py:379`), self/cls dispatch, and receiver-is-class steps were all
**dead for Java**. Every call fell through to global name matching, so
`userService.findById()` and `orderRepo.findById()` were indistinguishable.

Fix: variable→declared-type map from fields (collected before methods are walked,
since a method may call through a field declared later), method parameters, and
locals (including `var x = new Foo()`); `recv`/`recv_type` now passed on every
CALLS ref. `RawRef.recv_type` already existed (`models.py:303`) and the resolver
already consumed it — only the extractor never filled it in.

Not covered: chained receivers (`a.b().c()`), casts, array access — these need
return-type propagation and still emit no receiver.

### 2.2 External-receiver suppression — `graph_core/resolver.py:~300`

When `recv_type` is known but is **not** an in-repo class (`Connection`, `List`,
`Logger`), no in-repo function can be the target. Previously such calls fell to
the step-5 arity fallback and fanned out across every same-named method in the
repo — 100% false by construction. Now returns `[], "external_receiver"` and the
CALLS branch emits nothing, counting it as `external`.

Measured: one call site on a JDBC receiver went from **200 false edges → 0**.

> **This is also why DB detection is currently impossible — see §4.3.**

### 2.3 Performance / memory (byte-identical)

| change | where | note |
|---|---|---|
| `(name,parent)` + `(name,file)` composite indices | `resolver.py` | only 1.03–1.09× — **steps 1-2 were not the bottleneck** |
| `funcs_by_name` precomputed | `resolver.py:142` | removes a per-ref list allocation |
| enum `.value` → module constants | `resolver.py:44-47` | 19 sites, ~1.18M descriptor calls removed |
| `_tail_name` rfind/slice | `resolver.py:~910` | was `split(".")[-1]`, ~493k list allocs |
| per-file import tails + lazy fqn-segment memo | `resolver.py` | was ~826k `str.split` per resolve |
| `gc.freeze()` around the ref loop, `unfreeze()` in `finally` | `resolver.py` | deliberately **not** `gc.disable()` — that leaks cycles unboundedly |
| `DEFINES` removed | all 3 extractors, `pipeline.py:_RETAINED_EDGE_TYPES` | exact duplicate of CONTAINS, **zero consumers** in either repo |

All verified against a fixed edge/node/coverage hash. `DEFINES` removal cut
extraction edges 50% (Python corpus 1304→652, Java 45→23).

### 2.4 javac resolver (new) — `graph_core/javac_resolver.py` + `scripts/oracle/CallOracle.java`

Resolves Java CALLS with the real compiler. **Needs a JDK but no build system** —
`-sourcepath` lets javac resolve in-repo types from source alone. External types
fail and are skipped, which is fine because the graph only holds in-repo targets.

Runs **before** resolve (`pipeline.py`, the `javac` stage), and resolve then skips
CALLS refs for the files javac covered (`resolve(skip_call_files=...)`). That
ordering matters: SCIP ran *after* resolve and replaced its output, which forced
every CALLS edge to be held in RAM (`defer_edge_types`) waiting to be superseded —
a latent OOM. `defer_edge_types` is now permanently empty.

Coverage is **per-file, not per-language**: javac owns the files it attributed,
the heuristic keeps the rest. No threshold cliff, no silent gaps. One quality
floor remains — `min_attribution_rate=0.5`: if javac bound <50% of the
invocations it saw, attribution itself broke and the whole pass is discarded.

Enabled via UI checkbox / `GRAPH_JAVAC_RESOLVER=1`. Knobs:
`GRAPH_JAVAC_TIMEOUT_SECONDS` (3600), `GRAPH_JAVAC_BATCH_SIZE` (400 — this is the
**memory** knob; lower it if javac OOMs rather than raising `-Xmx`).

### 2.5 SCIP removed entirely

`scip-java` indexes by compiling through Maven/Gradle. The target repo has **no
build files at all** (verified: no `pom.xml`/`build.gradle` anywhere within 3
levels, only an orphan `.mvn/` directory), and the zip upload strips them anyway
(`SUPPORTED_CODE_EXTS`, `upload_utils.py:11`). It could never run.

Deleted `scip_resolver.py`, `graph_core/scip/`, both pipeline stages, six config
accessors, and the `scip_only` measurement mode.

### 2.6 Polymorphic dispatch — made non-fatal + batched

A live run built a complete graph, then **the polymorphic pass threw and
`ensure_graph_indexed` wiped the whole namespace** (first ingest → `has_baseline
False` → `store.wipe`). Two fixes:

- `_run_polymorphic_cypher` (`pipeline.py:978`) now catches and logs. It's an
  enrichment running *after* the graph is durable; it must never destroy it.
- The Cypher was one unbatched transaction. Now `CALL (...) { ... } IN
  TRANSACTIONS OF 1000 ROWS`, matching its already-batched sibling
  `derive_overrides_cypher`. Count via before/after diff.

**Root cause unconfirmed** — the exception text was never captured. Batching is
the strong hypothesis (27k OVERRIDES fanning out, uncapped, in one transaction).

### 2.7 Verification tooling

- `scripts/oracle/CallOracle.java` — javac ground truth. Emits
  `callerClass/callerMethod/callerArity/calleeClass/calleeMethod/calleeArity/file/line`
  plus `@FILE` markers for every attributed file, and a `=== STATS ===` block.
- `scripts/oracle/compare_to_graph.py` — diffs oracle vs Neo4j into
  precision/recall, split by confidence.

---

## 3. Measured metrics (real repo, ~16.5k Java files)

### 3.1 Before/after §2.1 + §2.2

| | before | after |
|---|---:|---:|
| total edges | ~80M | ~15M |
| ambiguous CALLS | 62,588,669 | 4,785,154 |
| ambiguous REFERENCES | 231,700 | 8,364,726 |
| **total ambiguous** | **62.9M** | **13.2M** (−79%) |
| **resolved CALLS** | **357,288** | **592,955** (+66%) |
| resolve wall clock | ~3h | "so fast" |
| peak RSS | 3626 MB | 3626 MB (flat through write) |

Strategy shift: `name+arity` 61,996,299 → 4,034,842 (−93.5%); `receiver_type*`
**847 → 483,455** (570×); `polymorphic_dispatch` 4,637 → 122,688;
`same_scope*` and `imports_qualified*` unchanged (good edges preserved).

The ambiguous **percentage** stayed ~88% only because non-ambiguous edges also
left (`USES` 13.4M, `BELONGS_TO` 722k, `PASSES` 132k, `DEFINES` 702k — the first
three removed by the earlier phase-7 rework, not by this work). **Track absolute
counts, not the ratio.**

### 3.2 javac oracle vs graph — the decisive measurement

Oracle: 398,776 resolved invocations → 201,550 distinct (caller,callee) pairs
across 8,629 caller classes.

| | pairs | TP | FP | FN | precision | recall |
|---|---:|---:|---:|---:|---:|---:|
| ALL CALLS | 3,806,726 | 189,117 | 3,617,609 | 12,433 | **5.0%** | **93.8%** |
| confident (non-AMBIG) | 488,025 | 187,189 | 300,836 | 14,361 | 38.4% | 92.9% |
| AMBIGUOUS only | 3,318,701 | **1,928** | 3,316,773 | 199,622 | **0.1%** | 1.0% |

**Read this carefully — it drives everything:**

1. **Recall is good (93.8%).** Discovery works; the right answer is in the
   candidate set almost always.
2. **Precision is catastrophic (5%).** It can't choose between candidates.
3. **Ambiguous edges are worthless**: 3.3M pairs contain 1,928 correct edges and
   contribute 1.0% recall. Deleting all of them costs 0.9pt recall and gives
   7.7× precision with 87% fewer edges.
4. **Precision compounds along paths** — a *k*-hop path is correct with ~*p^k*.
   At 38% edge precision a 5-hop path is **0.8%** correct. Any path-based
   analysis is worthless until edges are precise.

Caveats: overloads collapse (same `fqn`, so hitting the wrong overload of the
right method scores as correct); only caller classes javac attributed are
compared; **the run predates the `-sourcepath` fix (§4.1), so coverage was
artificially capped** — rerun before trusting the absolute numbers.

---

## 4. Known gaps and bugs

### 4.1 `-sourcepath` was wrong (FIXED, needs re-measuring)

The oracle passed the **repo root** as `-sourcepath`. javac resolves
`com.foo.Bar` at `<sourcepath>/com/foo/Bar.java`, which fails for
`src/main/java/com/foo/Bar.java`. Proven directly:

```
sourcepath = repo root      →  error: package com.acme.a does not exist
sourcepath = src/main/java  →  clean compile
```

Only same-batch types resolved, capping coverage at whatever batching grouped
together. This almost certainly explains 8,629 caller classes out of ~20,000
files. Now derives real source roots from each file's `package` declaration
(`CallOracle.deriveSourceRoots`), handling multi-module layouts. **§3.2 must be
re-measured.**

### 4.2 Function nodes miss several constructs

`java.py:245` only walks `method_declaration`/`constructor_declaration` inside
`_TYPE_DECLS` (class/interface/enum/record). **No Function node** for:

- lambdas (`x -> doThing(x)`)
- **anonymous inner classes** (`new RowMapper() { ... }`) — `object_creation_expression`, not a `_TYPE_DECLS` type
- static/instance initializers (`static { }`)
- local classes declared in a method body

Calls inside these are attributed to the enclosing method or lost. **Directly
relevant to leak detection** — JDBC callbacks and cleanup often live in exactly
these constructs.

### 4.3 No database awareness at all

The Java extractor recognizes Spring endpoints, RestTemplate, auth annotations,
DI, events — **and nothing database-related**. No JDBC, JPA, `@Repository`,
`@Entity`, `EntityManager`, `JdbcTemplate`, MyBatis, Mongo/Redis/Cassandra.

Worse, §2.2 makes it *harder*: `conn.close()` / `getConnection()` have external
`recv_type`, so they now emit **nothing**. Previously they fanned out into
garbage. Either way the graph never records "this function closes a connection."

`Table` nodes only come from `.sql` DDL, and that path is dead twice:
`.sql` is not in `SUPPORTED_CODE_EXTS` (stripped at upload), and
`_derive_sql_links` (`pipeline.py:1161`) returns `[]` immediately with no
`Table` nodes. The target repo's SQL lives in Java string literals
(`AmsSqlQuery#getPalletQtyCounter`), not DDL.

Verify with: `MATCH (t:Table) RETURN count(t);` — expected **0**.

### 4.4 Java field reads under-detected

`WRITES=51,867` vs `READS=3,521`. `java.py:361` only detects field access via
`_this_field`, i.e. explicit `this.x`. Bare `x` references to fields are missed.
If connections are stashed in fields (common in DAO code), they can't be traced.

### 4.5 `dfg_json` removed → taint analysis silently broken

The phase-7 rework deleted `dataflow.py`, which wrote the `dfg_json` node
property. **`analyzer/taint.py:112,140` and `analyzer/agents.py:296,413` read
it.** Without it `_has_sink_flows` returns False for everything. The production
copy still has `dataflow.py`; the sandbox does not.

### 4.6 Other

- **`fingerprints/` are stale** — all 8 files carry pre-change `DEFINES` counts and `edge_set_hash`. Regold via `scripts/graph_fingerprint.py` **after** validating a real run.
- **`OVERRIDES` has zero analyzer consumers** — its only reader is the polymorphic Cypher pass. It's `INFERRED` (name+arity only), so false OVERRIDES → false polymorphic CALLS.
- **`javascript.py` never sets `recv_type`** — same bug Java had, unfixed.
- **javac CALLS omits `new Foo()`** — `CallOracle` visits `MethodInvocationTree` only, not `NewClassTree`. Constructor invocation is `INSTANTIATES` (heuristic) instead.
- **Extraction workers crashed** on the real run (`BrokenProcessPool` → sequential fallback). Cause never captured; search logs for `crashed mid-batch`. Possibly OOM.
- **Write workers > 1 deadlock** on concurrent `MERGE`. Stuck at 1. Fix = partition batches by node-id hash.

---

## 5. Planned changes (priority order)

### P1 — External call facts (unblocks the main goal)

Record calls whose receiver type is external, instead of discarding them. Either
`CALLS_EXTERNAL` → synthesized `External` node keyed `Connection#close`, or a
marker on the calling Function. Detection already exists — the information is
currently thrown away in `resolver.py`.

Per-language pattern tables classifying them:

```
acquire   getConnection, createStatement, prepareStatement, getSession
execute   executeQuery, executeUpdate, execute, executeBatch
release   close, commit, rollback
JPA/ORM   find, save, persist, merge, remove, createQuery, getResultList
python    connect, cursor, execute, commit, rollback, close,
          session.query/add/commit, objects.filter/get/save, insert_one, find
ts/js     knex, prisma, mongoose, pg, typeorm equivalents
```

Enables:

```cypher
MATCH (f:Function)-[:CALLS_EXTERNAL]->(:External {kind:'db_acquire'})
WHERE NOT (f)-[:CALLS*0..3]->(:Function)-[:CALLS_EXTERNAL]->(:External {kind:'db_release'})
RETURN f.file, f.name, f.start_line;
```

### P2 — Mark DB functions, let the LLM read them

**Do not** try to extract table names accurately. Dynamic query construction
makes regex produce confident garbage. Instead mark "this function does database
work" (+ optionally "builds queries dynamically") and let the LLM read the source.
Graph = recall, LLM = precision. Same reasoning that justified dropping
`dfg_json`.

### P3 — Inheritance-aware resolution

`methods_of_class[(class_id, name)]` holds a class's **own** methods only, so
calls to inherited methods miss and fall through. **8,236,060 of the 8.38M
REFERENCES carry `name+arity+unknown_recv`** = "receiver known, method not found"
= inheritance, in an `Abstract*`/`I*`-heavy codebase.

Needs two-phase resolve: pre-pass resolving `EXTENDS`/`IMPLEMENTS` refs via
`narrow_type` to build `ancestors_of` **before** the main loop (they're `RawRef`s
resolved in the same loop, so ordering would otherwise make results
input-order-dependent). Then walk ancestors nearest-first when a class's own
methods miss. Cost negligible (8,320 EXTENDS + 1,138 IMPLEMENTS vs 5M refs).

Watch the interaction with uncapped `polymorphic_dispatch` — it will expand every
newly-resolved ancestor call across all overrides, possibly re-growing the graph.

### P4 — Extract lambdas / anonymous classes (§4.2)

### P5 — Fix Java bare field reads (§4.4)

### P6 — Other languages

| language | tool | ceiling |
|---|---|---|
| TypeScript | `tsc` API — `createProgram()` + `TypeChecker.getSymbolAtLocation()` | high; **direct analogue of the javac work** |
| Python | Pyright (what `scip-python` wraps) | good, **not** 100% — duck typing, monkey patching, `getattr` |
| plain JS | tsc inference only | poor |

Cheaper interim win: give `javascript.py` the `recv_type` treatment Java just got.

### P7 — Cap or drop ambiguous CALLS

Measured: 0.1% precision, 1.0% recall contribution. Dropping costs 0.9pt recall
for 7.7× precision and 87% fewer edges. Consumers already truncate fan-out at
query time (`agents.py:343` `_FANOUT_GUARD`, `taint.py:1566` `_FANOUT_CAP`), so
much of it is discarded downstream anyway. Cap well above those values and
nothing any current consumer sees is lost.

---

## 6. developer_assistant changes

### 6.1 Port the graph_core work

`graph_engine/graph_core/` is a **separate vendored copy**. None of §2 applies
there yet. Note it still has `dataflow.py` (so `dfg_json` still works) — do not
blindly delete it, see §4.5.

### 6.2 Two-agent design

Intended: **Agent 1** reads the changed file only, no graph. **Agent 2** reads
call paths.

- Agent 1 is sound as-is: no graph dependency, always works, good baseline.
- Agent 2 must be **bounded and purposeful, not exhaustive**. "Every path end to
  end" is combinatorially explosive (path count grows exponentially with depth)
  and the existing code already refuses it (`_FANOUT_CAP`, `_FANOUT_GUARD`,
  bounded `CALLS*1..max_hops` in `scoring.py:27`).
- Scope it: start from changed functions, 2-3 hops, ranked by relevance (reaches
  a sink / crosses a trust boundary / touches shared state).
- **Filter to `r.strategy = 'javac_typed'`** where available. Per §3.2, at 38%
  edge precision a 5-hop path is 0.8% correct — feeding an LLM heuristic paths
  means feeding it fabricated call chains it will reason over confidently.

### 6.3 Retire or rework taint

`taint.py`/`agents.py` depend on `dfg_json` (§4.5). Either keep `dataflow.py` in
production, or complete the intended move to "Agent C reads the source of each
candidate path directly" (the reasoning already recorded in `pipeline.py`).

### 6.4 Node properties available to agents

Persisted (`models.py:100-140`): `name fqn repo kind lang file start_line
end_line visibility modifiers is_static is_abstract is_async return_type
param_count param_names signature docstring body_hash`.

**Not** persisted: `start_col/end_col`, `param_types`, `loc/cyclomatic/
branch_count/loop_count`, `extractor`, `package`, and `dfg_json`.

`file` + `start_line` + `end_line` is enough to locate and read source, which is
all either agent needs.

---

## 7. Accuracy summary

| | accuracy |
|---|---|
| `CONTAINS` | ~100% — pure syntax, no inference; only fails by omission |
| `Function` nodes | high; misses lambdas, anonymous classes, initializers, local classes |
| `CALLS` (Java + javac) | ~100% for method invocations; `new Foo()` is `INSTANTIATES` |
| `CALLS` (Python) | heuristic; has `recv_type` so better than Java was, still name-based |
| `CALLS` (JS/TS) | heuristic, no `recv_type` at all — weakest |
| `OVERRIDES` | `INFERRED` — name+arity only, can be wrong |
| `READS`/`WRITES` (fields) | writes ok, **reads under-detected** (§4.4) |
| `READS`/`WRITES` (tables) | **non-existent** (§4.3) |

---

## 8. Immediate next steps

1. **Commit.** Everything in §2 is uncommitted; `javac_resolver.py` and
   `scripts/oracle/` are untracked.
2. **Run with javac OFF** — validates §2.1–2.6 (the proven wins) and the SCIP
   removal refactor. Nothing here was integration-tested against Neo4j; 13 unit
   tests pass and the resolver is hash-identical, but the full pipeline was not
   run locally (no Neo4j on the dev box).
3. **Run with javac ON**, then re-measure §3.2 with the `-sourcepath` fix. Watch
   `javac.file_coverage`, `javac.attribution_rate`, `stage_seconds["javac"]` vs
   `["resolving"]`, and the `[oracle] derived N source root(s)` log line.
4. **Capture the polymorphic exception** if it recurs (§2.6) — now non-fatal, so
   the run survives and the error is logged.
5. Regold `fingerprints/` once a run is validated.
6. Then P1 → P2 (the DB/leak goal).

**Methodological note.** Every performance prediction made this session was
wrong at least once — composite indices (predicted 10×, measured 1.03×), the
"quadratic resolve" scaling curve (contaminated by cloned class names in the
synthetic corpus), SCIP viability (dead on arrival), Tier 2 percentages (below
the machine's noise floor). The *mechanisms* held up; the *magnitudes* did not.
Measure on the real repo before believing any number in this document that isn't
marked as measured.

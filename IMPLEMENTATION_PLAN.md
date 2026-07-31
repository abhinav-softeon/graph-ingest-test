# Implementation plan — precise call graphs for Java, JSP, JS, Python

Written 2026-07-30, for review before implementation. Companion to `HANDOFF.md`
(current state, measured metrics, known bugs). Nothing here is built yet.

**Goal:** every call correctly mapped caller→callee, correct classes/functions/
fields, correct READS/WRITES, plus database-access facts. Target ≥95% on
statically-bound calls. Virtual dispatch stays an explicitly-labelled
approximation — the LLM resolves it by reading source.

**Priority order (set by the owner):** Java + JSP → DB facts → JS → Python.
TypeScript deferred. Docker will carry whatever runtimes are needed (JDK,
Node.js); runtime weight is not a constraint.

---

## 1. Architectural decisions

These four decisions shape every phase. Disagreeing with any of them changes
the plan substantially, so they are stated up front.

### D1 — Bytecode resolves EDGES; tree-sitter owns NODES

The two sources have complementary, non-overlapping strengths:

| | exact | missing |
|---|---|---|
| tree-sitter | source positions, docstrings, modifiers, param names | call bindings (the whole problem) |
| bytecode | call bindings, field access, overload identity | source positions of the *declaration*, comments, param names |

So: **tree-sitter continues to build all nodes**, and the bytecode pass produces
`(caller_node_id, callee_node_id)` pairs by *matching* bytecode methods onto
existing tree-sitter nodes.

This is what guarantees the hard requirement that **every node carries real
`start_line`/`end_line`** — they come from the source AST, exactly as today.
Nodes that only bytecode knows about (lambdas, anonymous classes, `<clinit>`)
are synthesized with lines taken from the `LineNumberTable` (§D4).

It also means the bytecode pass slots in as a **resolver**, structurally
identical to `javac_resolver.py` — not as a new extractor. Far less new surface.

### D2 — Pure-Python class file parser, no JVM subprocess

`javac_resolver.py` shells out to a JVM and parses text output. For bytecode we
do not need to: the class file format is a stable, documented binary format, and
the subset needed for a call graph is small (§Phase 2.1).

| | pure Python | ASM (Java lib) |
|---|---|---|
| new runtime dependency | none | JAR to vendor + JVM startup per batch |
| runs in existing worker pool | yes, in-process | no, subprocess |
| edge-case robustness | our problem | battle-tested |
| debuggability from the pipeline | direct | across a process boundary |

Recommendation: **pure Python**. The parsing surface is bounded and the
operational simplification is large. Fallback to ASM only if class-file version
drift proves painful (§Risks).

### D3 — Per-file tier attribution, unchanged from the javac design

The existing design is right and generalizes. Each file is owned by the best
resolver that successfully attributed it; everything else keeps the heuristic:

```
Tier 0  bytecode          .class present and matched
Tier 1  javac / tsc       compiles, types resolve
Tier 2  heuristic+recv_type
Tier 3  heuristic name-only
```

Reuse `resolve(skip_call_files=...)`. No thresholds, no cliffs, no silent gaps.

**Keep the ordering lesson:** every precise resolver runs **before** `resolve`,
never after. Running after is what forced `defer_edge_types` and created the
latent OOM (HANDOFF §2.4). `defer_edge_types` stays permanently empty.

### D4 — Node identity is matched by lookup, never recomputed

`java.py:287` builds the Function id as:

```python
fqn = f"{class_fqn}#{name}"                       # NO params
mid = make_id(repo, f"{fqn}{params}", "method")   # params = RAW SOURCE TEXT
```

`params` is the literal source text — `(long id, String name)`, parameter names
and whitespace included. Bytecode has only the erased descriptor
`(JLjava/lang/String;)`. **The id is not reconstructible from bytecode.**

Therefore the bytecode pass builds a lookup index over already-extracted nodes:

```
(class_fqn, method_name, arity) -> [node_id, ...]
```

- unique hit → bind
- multiple hits (same-arity overloads) → compare erased param types against the
  descriptor, simple names only (`String` vs `java/lang/String`)
- still ambiguous → **do not guess**; record and fall through to the next tier

The ambiguity rate is a tracked metric, not a silent behaviour.

---

## 2. Cross-cutting invariants

Enforced in every phase; each gets a test.

1. **Every node has real `start_line` and `end_line`.** No synthesized node ships
   without them. Where bytecode is the only source, they come from
   `LineNumberTable`; if that attribute is absent the node is **not** created and
   the file falls to the next tier.
2. **Precise resolvers run before `resolve`.** Never after (D3).
3. **Oracle before resolver.** No resolver is built for a language until an
   independent ground-truth oracle exists and has produced a precision/recall
   number. This is what turned the project around once already (HANDOFF §3.2).
4. **Per-file attribution**, never per-language.
5. **Strategy is recorded on every edge.** Consumers must be able to filter
   `strategy = 'bytecode'` (HANDOFF §6.2). Precision compounds along paths, so
   a consumer that needs multi-hop reasoning must be able to demand Tier 0 only.
6. **Failure of an enrichment pass never destroys the graph** (HANDOFF §2.6).

---

## Phase 0 — Foundations

Small, unblocks everything. No behaviour change on its own.

### 0.1 Widen the upload allowlist

`ingest/upload_utils.py:11` admits source extensions only, so every artifact this
plan depends on is discarded before the pipeline sees it.

Add as a **filename allowlist for config** plus a small extension set — *not* a
blanket `.xml`/`.json` open, which would pull in test fixtures and lockfiles:

```
extensions : .class .jar .jsp .jspf .tag .tld .pyi .d.ts .sql
filenames  : pom.xml build.gradle build.gradle.kts settings.gradle
             web.xml tsconfig.json package.json pyproject.toml
             pyrightconfig.json .classpath MANIFEST.MF ivy.xml
```

Also raise/verify any per-file and total-size caps — `.jar` and `.class` change
the size profile of an upload significantly.

### 0.2 Classify artifacts separately from source

`discovery.py:27` `EXT_LANG` maps extension → language for *parsing*. The new
artifacts must not be fed to tree-sitter. Add a parallel `EXT_ARTIFACT` map
(`.class` → bytecode, `.jar` → archive, `.tld` → taglib, `web.xml` → deployment)
collected into a side channel on the discovery result, so extraction ignores them
and the new resolvers can find them.

### 0.3 Schema additions

`graph_core/schema.py`: add `"External"` to `NODE_LABELS`, `"CALLS_EXTERNAL"` to
`EDGE_TYPES`. Cheap now, needed by Phase 4. Check `validator.py` for assumptions
that every node has `file`/`start_line` — `External` nodes have neither
(`Endpoint` presumably already has this exemption; follow it).

**Exit criteria:** a zip containing `.class`, `.jar`, `web.xml`, `.jsp` survives
upload; discovery reports them as artifacts; extraction is byte-identical to
before (fingerprints unchanged).

**Risk:** widening the filter increases upload size and the untrusted-input
surface. `.jar`/`.class` are parsed, never executed — keep it that way.

---

## Phase 1 — Java bytecode oracle

**Oracle before resolver (invariant 3).** This phase answers "is bytecode
actually better, and by how much?" before any pipeline code is written.

### 1.1 `scripts/oracle/bytecode_oracle.py`

Standalone. Walks a directory of `.class` files (and `.jar`s) and emits the same
record shape `CallOracle.java` already emits, so `compare_to_graph.py` works
unchanged:

```
callerClass/callerMethod/callerArity/calleeClass/calleeMethod/calleeArity/file/line
```

plus a `=== STATS ===` block: classes parsed, methods, invocations by opcode,
`LineNumberTable` present/absent counts, synthetic/bridge skipped.

### 1.2 Three-way comparison

Run `compare_to_graph.py` for:

| source | question it answers |
|---|---|
| current graph vs bytecode | the real precision/recall baseline |
| javac oracle vs bytecode | do they agree? where do they diverge and why? |
| bytecode vs itself across two builds | is the artifact stable/current? |

The javac-vs-bytecode diff is the important one. Large divergence means either
the class files are stale relative to source, or javac coverage is worse than
believed. Either way it must be understood before Phase 2.

**Exit criteria:**
- Bytecode-derived precision/recall measured against the current graph.
- `LineNumberTable` present on ≥95% of methods (if not, D1's synthesis path is
  in trouble and Phase 2.4 needs rethinking).
- Class/method counts within a few % of tree-sitter's (a big gap means the
  class files don't correspond to the uploaded source — a blocking finding).

**Decision gate:** if bytecode coverage of the repo is low (few `.class` files,
or stale), **stop and reconsider** — fall back to javac + classpath JARs, and
Phase 2 becomes "improve javac coverage" instead.

---

## Phase 2 — Java bytecode resolver

The core of the plan. Subsumes HANDOFF §4.2 (lambdas/anon classes), §4.4 (bare
field reads), and most of P1.

### 2.1 `graph_core/bytecode/classfile.py` — the parser

Minimal, well-bounded subset of JVMS §4:

- header, `minor/major_version`
- **full constant pool** — must be parsed completely even for unused entries, or
  offsets desync. Handle `Utf8, Integer, Float, Long, Double, Class, String,
  Fieldref, Methodref, InterfaceMethodref, NameAndType, MethodHandle,
  MethodType, Dynamic, InvokeDynamic, Module, Package`
- `access_flags`, `this_class`, `super_class`, `interfaces`
- fields: name, descriptor, access flags
- methods: name, descriptor, access flags, attributes
- `Code` attribute → instruction scan for **only**:
  - `invokevirtual invokespecial invokestatic invokeinterface invokedynamic`
  - `getfield putfield getstatic putstatic`
  - `new` (→ INSTANTIATES)
- `LineNumberTable` (per method, per instruction offset)
- `Signature` (generics, if wanted later), `BootstrapMethods` (for
  `invokedynamic` → lambda target), `InnerClasses`, `SourceFile`
- **skip unknown attributes by declared length** — this is what makes the parser
  forward-compatible with new Java versions.

Instruction scanning needs the full opcode length table (including `wide` and the
variable-length `tableswitch`/`lookupswitch`) to step correctly.

### 2.2 `graph_core/bytecode/matcher.py` — bytecode → node identity

Per D4, a lookup index over extracted nodes. Handles:

| bytecode form | mapping |
|---|---|
| `com/acme/Foo` | → fqn `com.acme.Foo` |
| `Outer$Inner` | → `Outer.Inner` (verify against `java.py`'s nested-class fqn) |
| `Outer$1` (anonymous) | **no tree-sitter node** → synthesize (§2.4) |
| `lambda$doWork$0` | synthetic → synthesize, parent = `doWork` |
| `<init>` | → `constructor_declaration` node |
| `<clinit>` | no tree-sitter node → synthesize |
| `ACC_SYNTHETIC` / `ACC_BRIDGE` | **skip** — compiler-generated forwarding |
| overloads | arity, then erased simple-name param types |

Emits per-file counters: matched, unmatched-caller, unmatched-callee, ambiguous.

### 2.3 `graph_core/bytecode_resolver.py` — pipeline integration

Mirrors `javac_resolver.py` deliberately — same report shape, same
`attributed_files` contract, same `min_attribution_rate` quality floor:

- new `bytecode` stage in `pipeline.py:index_repo`, **before** `javac` and
  before `resolving`
- a file is attributed only when its class file parsed AND its caller methods
  matched; unattributed files fall through to javac, then the heuristic
- `BytecodeReport` with `file_coverage`, `match_rate`, `ambiguous_rate`,
  `stage_seconds`
- env knobs mirroring the javac ones: `GRAPH_BYTECODE_RESOLVER=1`,
  `GRAPH_BYTECODE_BATCH_SIZE` (the memory knob), plus a UI checkbox

Edges carry `strategy="bytecode"`, `confidence=EXTRACTED` — an observed binding,
not an inference.

### 2.4 Node synthesis for bytecode-only constructs

Closes HANDOFF §4.2. For anonymous classes, lambdas, `<clinit>`, and local
classes there is no tree-sitter node, so create one:

- `start_line` = min line in the method's `LineNumberTable`
- `end_line` = max line in the same table
- `file` = from the `SourceFile` attribute + package
- `kind` = `lambda` | `anonymous` | `initializer`
- `CONTAINS` from the enclosing class/method

**If `LineNumberTable` is absent, the node is not created** (invariant 1) and the
file falls to the next tier. Never emit a node with fake positions.

### 2.5 READS / WRITES from field instructions

Closes HANDOFF §4.4 exactly and precisely: `getfield`/`getstatic` → READS,
`putfield`/`putstatic` → WRITES, each carrying the **exact owning class and field
name** from the constant pool. No `this.x`-only limitation, no inference.

Expect the current `WRITES=51,867 / READS=3,521` imbalance to correct sharply —
that gap is the bug, and this measures whether it's fixed.

Note: compile-time constants (`static final int`) are inlined by javac, so reads
of them do not appear. Known, acceptable, worth documenting.

### 2.6 Tests

- parser round-trip on fixture class files (compile a small corpus in-repo)
- matcher: overload disambiguation, inner/anonymous/lambda naming, synthetic skip
- invariant 1: no synthesized node without lines
- integration: bytecode-attributed file produces zero heuristic CALLS

**Exit criteria:**
- Precision ≥95% and recall ≥ current 93.8% against the Phase 1 oracle
- Total edge count down (the 3.3M worthless ambiguous pairs should collapse)
- Every node has `start_line`/`end_line` — asserted, not assumed
- READS/WRITES ratio plausible
- Fingerprints regolded

**Risks:**
- *Stale class files* — bytecode may not match uploaded source. Detect by
  comparing class/method counts and `SourceFile`; report loudly rather than
  silently producing edges for code that no longer exists.
- *Class file version drift* — mitigated by skip-unknown-attributes-by-length.
- *`invokedynamic`* — lambda targets need `BootstrapMethods` resolution; string
  concatenation also compiles to `invokedynamic` and must be ignored.

---

## Phase 3 — JSP

Highest priority alongside Java, because **JSP is the entry-point layer** — every
request→logic→DB path starts here. Without it every path starts mid-way.

### 3.1 Precompiled JSP classes (free path)

In a deployed app, JSPs are often already compiled to
`WEB-INF/classes/org/apache/jsp/...`. If present, Phase 2 handles them as
ordinary classes at zero extra cost. **Check for this first** — it may make 3.2
unnecessary.

### 3.2 `JspC` compilation path

If not precompiled: run `org.apache.jasper.JspC` to convert `.jsp` → `.java`,
then compile. Needs `servlet-api.jar`, `jsp-api.jar`, EL API, and TLDs. Docker
can carry these.

### 3.3 Name and line mapping (the real work)

Generated servlets are hostile to a code graph:

- mangled class names (`index_jsp`) — must map back to `/WEB-INF/views/index.jsp`
- one enormous `_jspService` method containing the whole page
- line numbers point into generated `.java`, not the `.jsp`

**`SMAP` (JSR-45)** carries the `.jsp` → generated-line mapping when Jasper is
run with it enabled. Required, or nodes point at generated code no human can
open. If SMAP is unavailable this phase produces edges but poor locations —
which violates invariant 1 and must be treated as a blocker, not a nuisance.

### 3.4 `web.xml` → Endpoint nodes

Servlet mappings are URL → servlet/JSP. This is the endpoint layer, feeding the
existing `Endpoint`/`EXPOSES` model directly. High value, low effort, and
independent of 3.2/3.3.

### 3.5 `.tld` → tag handler links

TLDs map `<mytag:foo>` to a Java handler class. Without them custom tags cannot
be linked to code at all.

### 3.6 Direct `.jsp` scriptlet parsing (fallback)

If neither precompiled classes nor JspC is viable: extract `<% %>` / `<%= %>` /
`<%! %>` Java out of the `.jsp` and feed it to the Java extractor. Crude, loses
generated-servlet context, but needs no dependencies and gives real `.jsp` line
numbers directly. EL `${user.name}` → `getName()` can be modelled here too.

**Exit criteria:** JSPs appear as nodes with correct `.jsp` file paths and line
numbers; `web.xml` endpoints resolve to handlers; a request→DB path is traceable
end to end for at least one known route.

---

## Phase 4 — Database / external call facts (P1 + P2)

Deliberately placed after Phase 2, because **bytecode makes this dramatically
better and simpler.**

Today's design gates on `recv_type` inferred by the extractor, which needs a
pattern table over *type names* and is only as good as that inference. In
bytecode the owner is exact:

```
invokeinterface java/sql/Connection.close:()V
                └── exact owner, no inference ──┘
```

No guessing whether `close()` was on a `Connection` or an `InputStream`.

### 4.1 `graph_core/external_api.py`

Type-keyed classification (types first, methods second — a bare method-name table
would tag `inputStream.close()` as `db_release`, which is exactly the confident
garbage P2 warns against):

```
JDBC     java.sql.{Connection,Statement,PreparedStatement,CallableStatement,
                   ResultSet,DataSource}
Spring   JdbcTemplate NamedParameterJdbcTemplate TransactionTemplate
JPA      EntityManager Session SessionFactory Query TypedQuery
MyBatis  SqlSession SqlSessionFactory
```

then method → `db_acquire | db_execute | db_release`. Unknown method on a known
DB type → `db_other`, **not dropped**: "touches `Connection`" is itself signal.

Plus `external_id/fqn/display` helpers mirroring `apispec.py:64-73`.

### 4.2 External node + `CALLS_EXTERNAL` emission

Two emission sites, same classifier:

- **bytecode path** (precise) — owner is exact
- **heuristic path** — `resolver.py:736`, the `external_receiver` branch, which
  currently `return`s and discards the fact. Synthesize/dedupe an `External` node
  into `extra_nodes` (same pattern as the Endpoint synthesis at
  `resolver.py:705-714`) and append a `CALLS_EXTERNAL` edge. `cov.external += 1`
  either way, so coverage stays honest.

`CALLS_EXTERNAL` is deliberately **not** added to `_RETAINED_EDGE_TYPES` — no
derive pass reads it, so it streams to Neo4j and drops, per the existing memory
design.

### 4.3 Validate the leak query

```cypher
MATCH (f:Function)-[:CALLS_EXTERNAL]->(:External {kind:'db_acquire'})
WHERE NOT (f)-[:CALLS*0..3]->(:Function)-[:CALLS_EXTERNAL]->(:External {kind:'db_release'})
RETURN f.file, f.name, f.start_line
```

Validate against hand-checked known leaks *and* known-clean code. A leak
detector with false positives is worse than none.

### 4.4 P2 — mark, don't extract

Per HANDOFF P2: do **not** regex table names out of dynamically-built SQL. Mark
"this function does database work" (+ "builds queries dynamically") and let the
LLM read the source. Graph = recall, LLM = precision.

If MyBatis XML is now admitted (Phase 0.1), mapper XML *does* contain reliable
statement→SQL mappings — that is the one place static table extraction is sound.

**Exit criteria:** `MATCH (t:External) RETURN count(t)` non-zero and sane; leak
query returns hand-verifiable results; no `db_release` tag on non-DB `close()`.

---

## Phase 5 — JavaScript

### 5.1 `recv_type` in `javascript.py` (do this first, standalone)

`javascript.py` sets no `recv_type` at all (HANDOFF §4.6) — the exact bug Java
had. Porting the `java.py` treatment moves JS/TS from Tier 3 to Tier 2 with **no
new infra, no config files, no `node_modules`**. Best value-per-effort item in
the whole plan and independent of everything else.

### 5.2 Oracle — `tsc` TypeChecker dump

`tsc` checks `.js` too (`allowJs` + `checkJs`), so one oracle serves JS and TS.
Node script emitting the same record shape as the other oracles:

```
program = ts.createProgram(files, options)
checker.getResolvedSignature(callExpr)   // the call-resolution API
symbol.getDeclarations()                 // file + line of the definition
```

`getDeclarations()` gives exact source positions, so invariant 1 holds naturally.

### 5.3 `tsc`-based resolver, gated on 5.2's numbers

Requirements: `tsconfig.json` (mandatory — without it `paths`/`baseUrl` aliases
resolve to nothing and whole subtrees vanish), `node_modules` or `@types/*`,
`package.json`, monorepo config, Node.js runtime.

Expected ceiling: strict TS 90–95%; loose TS with `any` degrades fast; plain JS
40–60% on inference alone.

**Security note:** `npm install` on customer code runs `postinstall` scripts.
Use `--ignore-scripts`, sandbox it, or rely on committed `node_modules`/types
only. Decide before it is load-bearing.

---

## Phase 6 — Python (basic)

Owner has explicitly scoped this down: current heuristic quality is acceptable.

### 6.1 Do not pursue `.pyc`

`user.save()` compiles to `LOAD_METHOD "save"` — the method *name as a string*,
no owner. Python binds attributes at runtime against the object's MRO. `.pyc`
carries **exactly the information the AST already has**, in a harder-to-read
form. Dead end; documented so it isn't re-investigated.

### 6.2 Runtime-tracing oracle (cheap, high value)

`sys.monitoring` (3.12+) or `sys.settrace`: run the test suite, record the calls
that **actually happen**. Perfect precision on executed paths; recall bounded by
test coverage. The exact dual of static analysis, and the Python equivalent of
`CallOracle.java`.

Worth building even if no Python resolver follows — it is the only way to get a
real precision/recall number for the existing heuristic.

### 6.3 Pyright resolver — optional, gated on 6.2

Needs installed deps (venv/site-packages) or stubs, Python version, and import
roots (`pyproject.toml`/`pyrightconfig.json`). Note Pyright is a Node program, so
Python indexing needs the Node runtime too.

Ceiling: ~85–95% on annotated code with deps; 40–60% on untyped legacy. Never
100% — monkey patching, `getattr`, dynamic import, signature-rewriting
decorators, `**kwargs` forwarding, metaclasses.

**Same `pip install` security concern as npm** (`setup.py` executes on install).

---

## Phase 7 — TypeScript (deferred)

Mostly free once Phase 5 exists — the same `ts.createProgram` serves both. Revisit
only if TS becomes a meaningful share of the corpus.

---

## 3. Metrics tracked throughout

Per HANDOFF's methodological note — *every* magnitude prediction made previously
was wrong at least once, while the mechanisms held. Measure on the real repo.

| metric | why |
|---|---|
| precision / recall vs oracle, per tier | the only number that matters |
| **absolute** ambiguous edge count | the *ratio* misled before — track counts |
| edges by `strategy` | proves tier attribution works |
| per-tier file coverage | where each resolver actually applies |
| nodes missing `start_line` | must stay 0 (invariant 1) |
| READS/WRITES ratio | §4.4 regression signal |
| peak RSS + `stage_seconds` per stage | the OOM/latency wall |
| ambiguous-match rate in the matcher | silent-guessing signal |

---

## 4. Open questions to resolve before Phase 2

1. **Do `.class` files exist for the target repo, and how complete/current?**
   Blocks the whole plan's premise. `target/classes`, `build/classes`,
   `WEB-INF/classes`, or a `.war`/`.ear`.
2. **javac coverage from the last run** — `javac.file_coverage`,
   `javac.attribution_rate`, `[oracle] derived N source root(s)`. If already
   high, the JAR/classpath discussion is moot.
3. **Are JSPs precompiled?** Decides whether Phase 3 is nearly free or a real
   project.
4. **Does the repo use Lombok?** (`grep -r "@Data\|@Getter\|@Setter\|lombok"`).
   If yes, source-only analysis is structurally missing methods and bytecode
   moves from "better" to "required".
5. **Is `min_attribution_rate=0.5` right for bytecode?** Bytecode either parses
   or doesn't; a rate floor may be the wrong quality gate here.

---

## 5. Suggested sequencing

| order | phase | gate |
|---|---|---|
| 1 | 0 — foundations | fingerprints unchanged |
| 2 | 1 — bytecode oracle | **decision gate**: is bytecode viable? |
| 3 | 2 — bytecode resolver | ≥95% precision |
| 4 | 3.1 + 3.4 — JSP precompiled check + `web.xml` | cheap, high value |
| 5 | 4 — DB / external facts | leak query hand-verified |
| 6 | 3.2/3.3 — JspC + SMAP | only if 3.1 found nothing |
| 7 | 5.1 — JS `recv_type` | standalone, can be pulled forward any time |
| 8 | 5.2/5.3 — JS oracle + tsc resolver | |
| 9 | 6.2 — Python tracing oracle | |
| 10 | 6.3 / 7 — Pyright, TS | optional |

Phase 5.1 is independent of everything and can be done at any point as a quick
win if the Java work stalls on a blocking answer to §4.

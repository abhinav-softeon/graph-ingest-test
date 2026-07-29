# Graph ingestion: unconditional node/edge slimming + derive simplification

## Context

The graph-ingestion pipeline (`graph_build_test/graph_core/`, standalone sandbox
for the vendored `graph_core` inside `sail/services/developer_assistant`) holds
full `Node`/`Edge` Python objects in RAM through resolve → derive → write. On
the user's real corpus (20k files, ~900MB source) this takes 5 hours and peaks
at 45-50GB RAM, "working" only because macOS silently swaps instead of OOM-
killing. This is the exact scenario `RECENT_CHANGES.md`/`TIER3_MEMORY_PLAN.md`
(in the sister `developer_assistant` repo) already investigated and partly
fixed — but their own docs flag the remaining gap explicitly: *"peak memory
during resolve→derive→write is still 'the whole graph at once' ... the real
remaining lever, not yet done, is replacing full Node objects in the lookup
indices with slim id/name/fqn-only records."*

Two existing mechanisms exist for this, but neither solves it standalone:
- **Streaming ingest/writer** (`GRAPH_STREAMING_INGEST`/`GRAPH_STREAMING_WRITER`)
  — the exact "write early to Neo4j, keep only a slim projection in RAM"
  mechanism already exists in `pipeline.py`, fully implemented and reusing an
  already-correct property-patch write path (`store.write_semantics`). But it's
  gated behind `GRAPH_CHECKPOINT_ROOT` (disk-spill checkpointing) being
  configured — a dependency that isn't structurally required, it's just how it
  was originally wired.
- **`GRAPH_LOWRAM_DERIVE`** (Option A) — a separate, standalone disk-spill
  mechanism for derive specifically, hard-guarded as mutually exclusive with
  the above. The user found it unreliable on their machine.

Net effect today: resolve can be made memory-bounded (via streaming, if you
accept disk-spill checkpointing), but **derive gets no RAM benefit either
way** — it's always "hold everything" unless you fight the mutual-exclusion
guard, which the user has found doesn't work well for them.

**Goal of this change**: make node/edge slimming the *unconditional default*
— no flags, no disk-spill dependency — and additionally shrink what derive
needs to hold at all, based on a review of what the downstream analyzer layer
(`developer_assistant/app/services/code_review/graph_engine/analyzer/`)
actually consumes. Two derive passes (`_classify_roles` /
`_derive_module_ownership_and_uses`) turned out to be fully removable: their
outputs (`component_role` and `module_id`/`USES`) are either unused downstream
today or being intentionally superseded by planned changes to Agent A/B/C
(the user's own call, made with visibility into that separate roadmap — this
plan does not touch `developer_assistant`, only `graph_build_test`).

**Hard requirement**: the resulting graph must be the same and complete —
identical node/edge data reaching Neo4j (same properties, same edges), just
computed/written differently. Verified with a ported regression-oracle script,
not by inspection.

## Status (2026-07-29)

Implemented and Gate-verified (byte-identical on the small test corpora,
`graph_core/tests/` green throughout): **items #1, #2, #3, #6, #7**.

**Item #4 (Cypher migration of fan_in/fan_out, OVERRIDES, polymorphic-dispatch
CALLS) is HELD, not implemented** — found unsafe as originally scoped before
writing any Cypher (see its section below for why). `_attach_call_metrics`,
`_derive_overrides`, `_synthesize_polymorphic_calls` remain unchanged, in
Python, reading the in-RAM (now fully-slim) edge list. User decision: hold
item #4, ship everything else, decide on #4 later after seeing this land.

**Items #2b and #4b are SKIPPED as a consequence of #4 being held** — both
were premised on #4 also landing (see their sections below for the specific
blocker found in each: `validate_graph`'s full-edge-set dependency for 2b,
`all_nodes` already being fully resident regardless for 4b). Not "not yet
done" — actively concluded not worth doing while #4 is held.

**Item #5 (`GRAPH_LOWRAM_DERIVE` guard fix) remains deferred**, per its
original sequencing note (measure real RAM first). One added wrinkle: because
item #2 made `stream_writer` unconditionally `True`, the *existing* guard
(`if lowram: if stream_writer or is_streaming_ingest_enabled(): raise
RuntimeError(...)`) now makes `GRAPH_LOWRAM_DERIVE` permanently unreachable —
setting it raises immediately instead of running. This is called out in
`CONFIGURATION.md` and the Streamlit UI's help text so it doesn't surprise
anyone; fixing it is still gated on the same real-corpus measurement as
before, now also needing a decision on item #4.

**Item #8 (new, added after the plan's original scope): removed
`fan_in`/`fan_out`/`recursive` and `_attach_call_metrics` entirely — DONE.**
Prompted by the user asking, after item #4's fan_in/fan_out multiplicity
problem was explained, whether these properties were even used downstream.
Checked (two independent full-repo greps of `developer_assistant`, including
frontend/schemas, not just the analyzer layer): zero consumers. Agent B's own
blast-radius/taint-chain code (`scoring.py`'s `blast_radius`, `agents.py`'s
`_batch_hop2_summaries`, `taint.py`'s `_ranked_paths_for_function`) each
independently compute their own live Cypher `count()`/walk over `CALLS` at
query time — none of them read the persisted `n.fan_in`/`n.fan_out`/
`n.recursive` properties. Confirmed `OVERRIDES` and polymorphic-dispatch
`CALLS` (the rest of item #4) are NOT in the same boat — `taint.py` and
`agents.py` both actively read `r.strategy == "polymorphic_dispatch"` off
live `CALLS` edges for taint-chain correctness — so this only closes the
fan_in/fan_out third of item #4, not all of it.

Removed: the three `Node` fields, `_attach_call_metrics` (both call sites —
normal path and `_lowram_derive_and_write`), `lowram_derive.py`'s
`streaming_call_metrics` (was already dead code — never called by
`pipeline.py`, only its own unit test), `attach_call_metrics_cypher` from
`cypher_derive_reference.py`, and the corresponding test coverage/comments.
Verified via a Gate-A-style isolated-divergence check (an intentional
property removal, not expected to be byte-identical): captured under the
same fixed `fixed_java`/`fixed_py` tags against the existing item-#6
(`gateB6_candidate_*.json`) baseline — since item #7 has no pipeline
behavior, that capture doubles as "golden, pre-item-8." Result: **only**
`derived_props_hash` diverged on both corpora; node counts, edge counts,
`edge_set_hash`, and `coverage` were all identical. `graph_core/tests/` green
(12/12 — one test removed since it tested the now-deleted function).

**Not yet done**: a real-corpus (20k-file) peak-RSS measurement of items
#1/#2/#3/#6/#7/#8 together, to (a) confirm the actual RAM improvement and (b)
inform the #4/#5 decision.

## Knowledge loss / accuracy — explicit accounting

- **Bulky node/edge fields, fan_in/fan_out relocation**: zero loss. Same data
  reaches Neo4j either way, just at a different time / computed in a different
  place (Python vs Cypher). Enforced by Gate B being byte-identical.
- **`module_id`/`BELONGS_TO`/`USES`/`cross_module`/`intra_module`**: removes a
  *materialized* artifact, not underlying information — `USES` was always a
  rollup of edges that still exist (`CALLS`/`READS`/`WRITES`/etc.), so
  "does A depend on B" stays answerable via a live Cypher query if ever
  needed later. Confirmed zero consumers anywhere in the service today
  (exhaustive sweep: analyzer/, REST API layer, frontend, the other two_agent
  engine, schemas/models — all clean).
- **`component_role`/`role_source`/`role_confidence`**: `role_source`/
  `role_confidence` are pure decoration everywhere, zero loss. `component_role`
  itself is real, confirmed functional loss *relative to today's deployed
  analyzer* — it currently drives taint-chain seeding (`_is_taint_source`),
  Agent C's entire architecture/layering pass (`collect_path_shapes`), and
  severity scoring (`endpoint_reachable`/`reach_factor`, an auth-finding
  severity cap) in `developer_assistant`'s analyzer layer. This is a deliberate
  user decision made with full visibility into that dependency, contingent on
  separately-planned changes to Agent A/B/C that would replace its function
  (not part of this plan — this plan only touches `graph_build_test`).
  **Sequencing risk**: don't ship this ingestion-side removal before the
  analyzer-side replacement is at least designed — otherwise there's a window
  where the graph lacks data the live analyzer still expects, which degrades
  silently (fewer findings, lower scores) rather than erroring.
- **Resolution accuracy is untouched**: none of resolve's matching logic
  (`narrow_call`/`narrow_type`/arity/import/package precedence) changes.
  `EXTENDS`/`IMPLEMENTS` resolution (heuristic name→node matching, precedence
  chain) stays in Python inside resolve() unchanged — it is NOT produced by
  tree-sitter (which only sees raw per-file syntax, e.g. "extends Foo" as a
  name string) and is NOT part of derive either; it's resolve's own job, kept
  exactly as-is. What DOES move to Cypher (item #4) is `OVERRIDES` — a
  separate, later, genuinely derive-stage fact computed by walking the
  *already-resolved* `EXTENDS`/`IMPLEMENTS` hierarchy to match method
  name+arity — a graph traversal over existing edges, not a new resolution
  step, so no accuracy change expected, but this is the part of the plan that
  most needs Gate B's byte-identical check (translating real BFS/cap/dedup
  logic into Cypher is the highest-risk piece here, not just relocating
  already-simple data).

**Noted for later, not part of this plan**: `_classify_roles` already treats a
function's `EXPOSES` edge (a direct, high-confidence structural fact from its
route decorator, present at extraction time regardless of this change) as
stronger evidence than its annotation/name/package heuristics. If the planned
Agent B rework changes `_is_taint_source` to check for an `EXPOSES` edge
instead of `component_role`, it recovers most of today's taint-seeding
capability from data that's already in the graph — arguably more accurate
than the current heuristic, at the cost of losing `_classify_roles`'s fallback
for methods on a Controller-tagged class with no directly-recognized route
decorator (a real but likely small edge case).

## What changes

### 1. Node early-write, unconditional (`graph_core/pipeline.py`)

Today (~L211-226):
```python
stream_refs = spilling and is_streaming_ingest_enabled()
stream_nodes = stream_refs                      # same boolean as stream_refs
stream_writer = stream_nodes and is_streaming_writer_enabled()
```
`stream_nodes` — writing all nodes to Neo4j right after extraction, then
clearing bulky/unused fields from every Node in RAM (~L518-532) — is
currently tied to `stream_refs`/disk-spilling for no structural reason. Split
them apart:
- **Expand the cleared-field list** beyond the historical 4
  (`docstring`/`signature`/`param_types`/`return_type`). The full
  never-read-downstream field audit also found: `end_col` (note: `end_line`
  IS still needed — by `scip_resolver.py` and `_derive_sql_links` — keep it,
  only `end_col` goes), `display_name`, `visibility`, `modifiers` (a
  `list[str]` — real per-instance cost), `is_static`, `is_abstract`,
  `is_async`, `host`, `loc`, `cyclomatic`, `branch_count`, `loop_count`,
  `is_lock`. Same pattern, same safety — just a longer list.
- `stream_nodes` becomes **always True**, independent of `spilling`. This
  always-write-then-clear block runs unconditionally right after extraction.
  (Confirmed via full grep across resolver.py/scip_resolver.py/dataflow.py/
  pipeline.py's derive section: these 4 fields are never read by anything
  after extraction — only `Node.props()` at write time needs them, and by
  then they've already been written.)
- `stream_refs` (actual ref-streaming from disk during resolve) stays a
  **separate, still-optional** flag, still gated on `spilling`/
  `GRAPH_CHECKPOINT_ROOT` — untouched, this is a different, genuinely-disk-
  dependent lever not required for this fix.

### 2. Edge early-write, unconditional (`graph_core/pipeline.py`)

`stream_writer` — write resolved edges to Neo4j right after resolve (deferring
`PASSES` always and `CALLS` when SCIP may run, via the existing
`defer_edge_types` set), then convert `all_edges` to `SlimEdge` (drops
`arg_names`/`flow_*`/`origin`/`extractor`/`strategy` — confirmed never read
again downstream of their creation) — becomes **always True**, no longer
gated on `stream_nodes`'s old value. Structurally safe because node early-
write (#1) always runs first now, so edge endpoints always already exist in
Neo4j when edges are written (this ordering dependency is *why* `stream_writer`
was gated on `stream_nodes` in the first place — preserved, just both sides
of that dependency are now unconditional instead of both being optional).

### 2b. Drop the in-RAM edge list entirely after the early write — SKIPPED

Originally scoped as "filter down to the 5 edge types kept passes still
read" — but with item #4 below moving `_derive_overrides` and
`_synthesize_polymorphic_calls` to Cypher too, **no Python-side derive pass
needs any edge in RAM at all anymore** (only `_attach_call_metrics`,
`_derive_overrides`, `_synthesize_polymorphic_calls` ever read edges, and all
three move to Cypher in item #4). Once #2's early write makes every edge
durable in Neo4j, `all_edges` can be dropped from RAM entirely right after
that write — free RAM savings (no disk, no speed tradeoff). `_build_package_tree`
and `_derive_sql_links` need nodes only, never edges. **One thing to verify
during implementation, not assumed**: whether `validate_graph` reads edges
for its count/invariant checks — if it does, either keep a minimal edge
count/id set for it or move its checks to Cypher counts too (`TIER3_MEMORY_PLAN.md`
already scoped this as an easy Cypher target).

**Outcome: skipped.** Verified `validate_graph(all_nodes, all_edges)` (the
LAST derive step before write) reads the *full* edge set — every type, not
just the 5 — for both dangling-edge detection (src/dst existence) and
per-type relation counts (`REQUIRED_RELATIONS` warnings, `stats.relations`).
Since it already needs everything right before write regardless, filtering
`all_edges` earlier in derive doesn't lower peak RAM (the peak — the full
SlimEdge list right after item #2's early write — already happens before any
filtering could apply) and only adds risk (reconstructing the full set for
`validate_graph`, or migrating its checks to Cypher too — which is exactly
the kind of change item #4 was held to avoid). Left `all_edges` as the full
list, unchanged from items #1/#2.

### 3. Remove two derive passes entirely

- **`_derive_module_ownership_and_uses`** (Module nodes, `BELONGS_TO` edges,
  `USES` edges) — deleted. Confirmed via full read of the `analyzer/` layer:
  nothing reads `module_id` or a `USES` edge's `cross_module`/`intra_module`
  tag, anywhere, for any purpose. Purely derive-stage (never touches resolve),
  self-contained, safe to remove cleanly. Its config knobs (`module_root_depth`,
  `module_roots`) and call sites (both the normal and `_lowram_derive_and_write`
  paths) are removed too.
- **`_classify_roles`** (`component_role`/`role_source`/`role_confidence`) —
  deleted, per the user's explicit decision after reviewing what currently
  depends on it (Agent B's endpoint-rooted taint-chain seeding, Agent C's
  architecture/layering pass, and severity scoring for non-endpoint-agnostic
  findings) — superseded by planned changes to Agent A/B/C in
  `developer_assistant` (out of scope here). Also purely derive-stage,
  self-contained, cleanly removable.

Kept as Python (still needed, still self-contained, need nodes not edges):
`_build_package_tree`, `_derive_sql_links` (see #4b), `validate_graph`
(pending the edge-need check noted in #2b).

### 4. Migrate every edge-dependent derive pass to Cypher — HELD, not implemented

**Outcome: held before writing any Cypher.** Found unsafe as scoped, via
code-comment archaeology in `pipeline.py` plus reasoning about Neo4j
semantics, not by trial-and-error:
- `fan_in`/`fan_out` via a Cypher `count()` would silently lose CALLS edge
  **multiplicity** (duplicate call sites are real signal) because
  `write_edges()` uses `MERGE (a)-[r:TYPE]->(b)`, which dedups relationships
  of the same type between the same node pair. This was already tried and
  reverted once by the original team — there's a standing code comment in
  `pipeline.py` documenting exactly this ("Why derive reads slim IN-RAM edges
  and NOT Neo4j (tried and reverted)").
- Polymorphic dispatch's fan-out cap (`_POLY_FANOUT_GUARD = 25`, "first 25
  callers in append order") has no reproducible Cypher equivalent without a
  stored ordinal on each edge — Cypher result ordering isn't guaranteed to
  match Python list-append order, so this risks non-deterministic divergence
  on any repo with >25 callers of an ancestor method (small enough that the
  test corpora here wouldn't catch it).
- `OVERRIDES` alone would be safe to migrate, but it's entangled with
  polymorphic dispatch (which reads `OVERRIDES` edges it produces), so a
  partial migration isn't worthwhile on its own.

`_attach_call_metrics`, `_derive_overrides`, `_synthesize_polymorphic_calls`
remain unchanged in Python. See the Status section above for what this holds
back (items #2b, #4b) and the sequencing implication for item #5.

Original scoping, kept for reference if this is revisited later:

All three remaining passes that read edges — `_attach_call_metrics`
(`fan_in`/`fan_out`/`recursive`), `_derive_overrides` (`OVERRIDES` edges),
`_synthesize_polymorphic_calls` (synthetic dispatch `CALLS` edges) — move to
Cypher, since by this point every edge they'd need is already durable in
Neo4j (from #2). This is what makes #2b's "drop all edges from RAM" possible.

- **`fan_in`/`fan_out`/`recursive`** — simplest, a pure count:
  ```cypher
  MATCH (f:Function)-[:CALLS]->() WITH f, count(*) AS fan_out ...
  MATCH ()-[:CALLS]->(f:Function) WITH f, count(*) AS fan_in ...
  // recursive: MATCH (f:Function)-[:CALLS]->(f)
  ```
- **`OVERRIDES`** — a variable-length path match over the *already-resolved*
  `EXTENDS`/`IMPLEMENTS` hierarchy (see the tree-sitter-vs-derive note above —
  this only touches the derive-stage override computation, not
  `EXTENDS`/`IMPLEMENTS` resolution itself, which stays in Python/resolve()
  unchanged) plus same-name+same-arity method matching. Needs care to
  reproduce the current BFS-with-multiple-inheritance-dedup behavior exactly.
- **Polymorphic dispatch `CALLS`** — needs the existing fan-out cap
  (`_POLY_FANOUT_GUARD = 25`) and dedup logic (`existing_into_children`)
  reproduced in Cypher (e.g. `ORDER BY` + `LIMIT` for the cap, then a
  existence check before creating each synthetic edge) — **the trickiest
  piece to get byte-identical**; verify this one especially carefully against
  Gate B.

All three still patch results back via the existing `store.write_semantics`
(same mechanism already used for `dfg_json`/etc.) — no new write path needed,
just new Cypher read+aggregate queries feeding into it.

### 4b. Scope `_derive_sql_links` to just Function+Table nodes — SKIPPED

Currently receives the full node list even though it only ever reads
`Function` and `Table` labeled nodes. Once nodes are already durable in Neo4j
(item #1), query just those two labels from Neo4j instead of scanning the
full in-RAM node list — small, free trim, same spirit as #2b.

**Outcome: skipped.** Unlike edges, `all_nodes` was never dropped from RAM —
it's needed in full through the entire derive stage regardless (`shared_by_id`,
`_build_package_tree`, `validate_graph`, etc. all need it). So querying Neo4j
for just Function+Table nodes instead of reading the already-in-RAM list
saves zero RAM and only adds a network round-trip and a new failure mode.
This item's premise assumed a world where nodes had also been dropped from
RAM by this point, which never applied even independent of item #4 being
held. `_derive_sql_links` left unchanged.

### 5. Fix `GRAPH_LOWRAM_DERIVE`'s guard (keep as safety valve — deferred until measured)

**Sequencing note**: implement #1/#2/#2b/#3/#4/#4b/#6 first, then measure real
peak RAM on the actual corpus (existing `runs/*.json` harness). Only
implement this item if that measurement shows RAM is still too high —
`GRAPH_LOWRAM_DERIVE` trades speed for RAM via real disk I/O, not worth
paying for without evidence it's needed. Kept here as a ready-to-go design,
not as a required step in the first implementation pass.

Currently: `if lowram: if stream_writer or stream_refs: raise RuntimeError(...)`.
Since `stream_writer` now defaults to True unconditionally, this guard would
make `GRAPH_LOWRAM_DERIVE` permanently unreachable. Per the user's decision
(keep it as an extra safety valve for extreme-scale graphs, not remove it):
adjust `_lowram_derive_and_write`'s entry path so it composes with the new
default rather than conflicting with it. Note this option gets even less
necessary than originally scoped now that #2b/#4 mean Python holds
essentially no edges during derive at all (not just slim ones) — if
`GRAPH_LOWRAM_DERIVE` is still needed after measuring, it would now mainly be
spilling the *ref list* or extraction-time working set for extreme-scale
cases, not derive's edges (derive barely holds edges to spill anymore). Guard
becomes: incompatible only with `stream_refs`/SCIP/incremental mode (its
existing, still-valid restrictions), not with the now-default node/edge
slimming.

### 6. Remove the old non-streaming code path entirely — DONE

Per "entire architecture change," not "wider flag surface": delete the
`else` branches in `index_repo()` that currently run when `stream_nodes`/
`stream_writer` are False (the old "hold everything, write once at the end"
path) — there is only one path now.

**Implemented as:** removed the two dead-code `else` blocks in the final
Neo4j-write section (the `stream_nodes`/`stream_writer` False branches around
the old `store.write_nodes(all_nodes...)`/`store.write_edges(all_edges...)`
whole-graph write, and the redundant `store.bootstrap()`/incremental/wipe
handling already done earlier under the streaming path). Left the `if
stream_nodes:`/`if stream_writer:` wrapper conditionals themselves in place
(they always evaluate `True` now, but removing them would mean re-indenting
~300 lines for a purely cosmetic gain, unlike raising the risk of the
individual-item verification approach this whole plan is built on) — the
actual alternate-behavior code is gone, which is what "one path now" meant.
Verified: `graph_core/tests/` green (13/13), Gate B fingerprint `IDENTICAL`
on both corpora against the fixed-tag goldens.

### 7. Config / UI / docs cleanup — DONE (scoped to what actually shipped)

- `graph_core/config.py`: `is_streaming_writer_enabled()`'s docstring updated
  to note it's no longer read by `pipeline.py` (kept only because
  `ingest/build.py`'s diagnostic status dict still reports it).
  `is_streaming_ingest_enabled()`'s docstring updated to describe its actual
  remaining role (slim resolve projection + `stream_refs`), not the stale
  "original whole-graph-in-memory path" wording. `get_lowram_derive()`'s
  docstring now documents the guard-unreachability issue from the Status
  section above. (`module_root_depth`/`module_roots` were already gone —
  removed as part of item #3, no separate cleanup needed here.)
- `cli.py`/`api.py`/`ui/app.py`/`ingest/toggles.py`: removed the
  `--streaming-writer`/`streaming_writer` flag/checkbox/form-field/env-var
  entirely across all three entrypoints (it's genuinely inert now, not just
  hidden) — `GRAPH_STREAMING_WRITER` is no longer set by any of this app's
  toggles. Updated the "Streaming ingest" checkbox's help text, and the
  "Low-RAM derive" checkbox's help text to warn it currently always raises
  (see item #5's guard-unreachability note).
- `CONFIGURATION.md`: rewrote the ingestion-mode table and the "least-resource
  configuration" example (the old one relied on `GRAPH_LOWRAM_DERIVE=true` +
  `GRAPH_STREAMING_WRITER=false`, which now always raises immediately — that
  example was actively wrong, not just stale) to reflect that node/edge
  slimming is unconditional and `GRAPH_LOWRAM_DERIVE` is currently
  non-functional. Flagged that the RAM numbers in that doc predate this
  change and haven't been re-measured yet.

Not part of this pass (scope tracks what #1-#3/#6 actually changed, not the
original item #4-contingent wording): no Cypher query methods were added to
`store.py` (item #4 held), no `_derive_sql_links` node-fetch scoping (item
#4b skipped), no edge-count/dangling-check migration for `validate_graph`
(would've been needed for item #2b, which is skipped).

## Verification (two-gate approach, since one change is intentionally NOT identical)

Port `graph_fingerprint.py` from
`sail/services/developer_assistant/app/services/code_review/graph_engine/scripts/graph_fingerprint.py`
into `graph_build_test/scripts/graph_fingerprint.py`, adjusting imports to
drop the `app.services.code_review.graph_engine.` prefix (it only depends on
`graph_core.config.Neo4jConfig`, `graph_core.pipeline.index_repo`,
`graph_core.store.GraphStore`, and the `neo4j` driver directly — no other
`developer_assistant`-specific code). This is the same oracle the original
team used to validate this exact class of change: reads the final graph state
back from Neo4j (node counts by label, edge counts by type, an order-
independent edge-set hash, resolver `Coverage`, a derived-properties hash) and
diffs two captures.

Because removing `_classify_roles`/`_derive_module_ownership_and_uses` is a
deliberate, expected divergence (fewer edge types, different derived-props
hash), run verification in two sequential gates rather than one combined
diff:

- **Gate A — derive-scope removal, isolated.** On current `main`, capture a
  golden fingerprint on 2-3 small representative corpora (propose using a
  small subset of `graph_build_test/graph_core/` itself as a zero-setup Python
  corpus, per the original team's own approach in `TIER3_MEMORY_PLAN.md`,
  plus one Java-containing corpus if easily available). Apply *only* change
  #3 (remove the two derive passes). Re-run, re-fingerprint, compare. Expected
  diffs: `edge_counts_by_type` loses `BELONGS_TO`/`USES`, `derived_props_hash`
  changes (no more `component_role`/`module_id`). Manually confirm *no other*
  field diverges — node counts, all other edge types/counts, `edge_set_hash`
  restricted to non-`USES`/`BELONGS_TO` types, and `coverage` must be
  unchanged. This isolates and validates the intentional removal.
- **Gate B — slimming, must be byte-identical.** Capture a *new* golden
  fingerprint on the Gate-A-passed code (this is the new baseline, captured
  under fixed repo tags `fixed_java`/`fixed_py` — see the repo-tag gotcha
  note below). Apply changes #1, #2, #6 (the items that actually shipped —
  #2b/#4/#4b/#5 held/skipped, see Status section). Re-run, re-fingerprint,
  compare against the Gate-B golden. **Result: `IDENTICAL`, zero divergence,
  on both the Java and Python test corpora**, for both the #1+#2 candidate
  and the later #6 candidate.
  - **Repo-tag gotcha, hit and fixed during this gate**: node ids are
    `sha1(repo + kind + fqn)`, so comparing fingerprints captured under
    *different* repo tags always mismatches on `edge_set_hash`/
    `derived_props_hash` even for structurally identical graphs — counts
    matching while hashes diverge is the tell. Always capture golden and
    candidate under the *same* repo tag.

Additionally:
- Run the existing `graph_core/tests/` suite (`test_slim_node.py`,
  `test_slim_edge.py`, `test_lowram_derive.py`) after each gate — must stay
  green.
- Re-measure peak RSS during a representative ingest (same
  `instrumentation`/`runs/*.json` harness already in place) before/after to
  quantify the actual improvement, alongside the correctness gates.
- The user's real 20k-file/900MB corpus is a 5-hour run — not part of the
  iterative gate loop above, but worth one final confirmation run once Gates
  A and B are both green on the small corpora, to confirm the RAM number
  actually comes down at real scale.

## Files touched (actual, as shipped)

- `graph_core/pipeline.py` — items #1/#2 (stream_nodes/stream_writer
  unconditional, expanded node-field clearing), item #3 (removed
  `_classify_roles`/`_derive_module_ownership_and_uses` and call sites), item
  #6 (removed the two dead `else` branches for the old whole-graph write
  path), unused `is_streaming_writer_enabled` import removed. `store.py`,
  `lowram_derive.py`'s internals, and `_derive_sql_links`/`_attach_call_metrics`/
  `_derive_overrides`/`_synthesize_polymorphic_calls` are **unchanged** —
  items #4/#2b/#4b were held/skipped, not partially applied.
- `graph_core/config.py` — docstrings updated for `is_streaming_ingest_enabled`,
  `is_streaming_writer_enabled`, `get_lowram_derive` (item #7). No function
  signatures changed.
- `cli.py`, `api.py`, `ui/app.py`, `ingest/toggles.py` — removed the
  `streaming_writer` flag/checkbox/form-field/env-var across all three
  entrypoints (item #7).
- `CONFIGURATION.md` — rewrote the ingestion-mode table and least-resource
  example to match current behavior (item #7).
- `scripts/graph_fingerprint.py`, `scripts/__init__.py` — new, ported from
  the sister repo, used for Gate A/B verification.
- `test_corpora/java_sample/`, `test_corpora/python_sample/` — new, the two
  fixed test corpora used for both gates (Java hand-authored; Python a frozen
  `git archive` snapshot of `graph_core/` itself, since the live directory is
  being edited during this work).

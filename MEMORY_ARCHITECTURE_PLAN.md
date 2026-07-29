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

**Item #17 (new): resolver nested indices flattened — DONE.** With the edge and
node lists dealt with, the resolver's own lookup indices became the largest
thing at the peak. Measured per entry: plain dict 31 B, `defaultdict(list)`
119 B, `defaultdict(lambda: defaultdict(list))` **311 B**.

The three nested ones — `methods_of_class`, `fields_of_class`, `fields_of_file`
— went from `{owner: {name: [Node]}}` to a flat `{(owner, name): Node}`, with
the value promoted to a list only when a name is genuinely duplicated within its
owner. At one member per (owner, name), which is overwhelmingly the common case,
the old shape paid for an inner dict AND a one-element list. ~311 B → ~40 B per
member, so roughly 270 B saved per method/field node.

Two secondary fixes fell out. The indices were `defaultdict`s read as
`methods_of_class[cls_id]`, which **minted an empty inner dict for every
class-id probed and missed** — a plain dict with `.get()` cannot. And the
scattered `[owner].get(name, [])` reads are now a single `_members()` helper, so
there is one place that knows the storage shape.

Deliberately NOT touched: `by_name`/`classes_by_name`/`endpoints_by_key`
(119 B/entry). Same single-element-list waste, but their read sites are spread
through the matching logic, and this is the code where a mistake changes
resolution rather than just memory.

**Verified by resolution fingerprint, not just tests:** the full edge set
(order-independent hash over type/src/dst/confidence/strategy), every edge key,
every node id, and the per-reftype `Coverage` counts are **identical** before and
after on both corpora.

**Item #15 (new): `arg_names` removed — DONE.** Wanted for overload
disambiguation; it cannot do that. Argument *names* at a call site say nothing
about parameter *types*, and the job is already done: Java overloads are
**already distinct nodes** (`java.py:277` puts the parameter signature in the
id), and `_apply_arity` already narrows candidates on `call_arity` vs
`param_count`. Python has no overloads at all.

It was also never wired up: both extractors ran a per-call-site AST walk
(`_pass_arg_names`/`_call_arg_names`) and then **never passed the result to
`ref()`**, so `RawRef.arg_names` was always `[]` and every `Edge.arg_names` was
`None`. Removing it deletes that walk from the hottest phase. Resolution
fingerprint unchanged.

The one thing it *could* have supported — matching Python keyword arguments
against `param_names` to separate two same-name, same-arity functions — was
considered and declined. It is a real but narrow precision gain and would have
changed resolution output.

**Item #16 (new): `GRAPH_LOWRAM_DERIVE` deleted — DONE.** Fixed earlier this
same session (item #5), then obsoleted by items #12 and #2b: with nothing left
that reads the bulk edges, its disk spill became **write-only** — `len()` and
the `rmtree` that deleted it were the only remaining uses. It wrote 130M edges
to disk and deleted them unread.

Removed: `lowram_derive.py`, `_lowram_derive_and_write`, the admission checks,
`get_lowram_derive`/`get_edge_spill_dir`, the Streamlit controls, the
docker-compose and `.env.example` entries, and `test_lowram_derive.py` /
`test_lowram_tail.py` / `test_lowram_equivalence.py`.

Worth recording that the effort was not wasted: repairing the flag is what
surfaced the structural-vs-bulk edge split (`_LOWRAM_STRUCT_TYPES`), and that
split — renamed `_RETAINED_EDGE_TYPES` — is exactly the mechanism the default
path now uses. The flag was the prototype for item #2b.

Replaced by `test_pipeline_contract.py`, which keeps the invariants those tests
happened to pin (OVERRIDES really being derived from the *resolved* hierarchy;
the removed payload fields staying removed) and adds a direct guard that the
sink does not start retaining the bulk again — a regression nothing else would
notice, since the graph would still be correct, just gigabytes heavier.

**Item #14 (new): `all_nodes` is a slim projection — DONE.** The node floor,
which items #1–#13 never touched (they all attacked edges).

The pre-resolve write used to be followed by a field-BLANKING loop: it zeroed
the bulky values but kept the 40-slot `Node` shell alive to the end of the run.
It now PROJECTS instead — `all_nodes[i] = all_nodes[i].to_slim()`, in place so
each full object is freed as it goes rather than briefly holding both lists.

The bigger half of the win is second-order. `resolve_nodes` used to be a
**second** slim list built alongside the still-full `all_nodes`, so the longest
phase of the run held 360 B + 160 B per node simultaneously. Because the slim
record now carries the full post-resolve read set, `resolve_nodes = all_nodes`
and that duplication is gone:

```
BEFORE  full Node + separate slim projection   360 + 160 + 2 ptr = 536 B/node
AFTER   one slim record, reused by resolve           160 + 1 ptr = 168 B/node
        -> 368 B/node saved at the resolve peak, 200 B/node after
        -> at 5M nodes: 2.68 GB -> 0.84 GB
```

`SLIM_NODE_FIELDS` grew from 12 to 16. The four additions (`repo`,
`start_line`, `start_col`, `end_line`) are not used for resolution — they were
established by AST-scanning every post-resolve consumer for Load-context reads
of a `Node` field: `_derive_sql_links` needs repo/start_line/end_line,
`validate_graph` needs start_line/end_line, `scip_resolver` needs start_col.

`all_nodes` is now a MIXED list — `SlimNode` for extracted nodes, full `Node`
for the late ones (resolve's synthesized nodes, package nodes, SQL Table nodes),
which must stay full because they have not been written yet and
`store.write_nodes` needs `.props()`. Verified that no `.props()` call can reach
a slim record: the only `write_nodes(all_nodes)` runs BEFORE the projection, and
every other call takes `late_nodes` or the sink's `extra_nodes`.

**The guardrail is what makes this safe.** `test_slim_node.py` previously
AST-scanned only `resolver.py`; it now also scans `_derive_overrides`,
`_build_package_tree`, `_derive_sql_links`, `_lowram_derive_and_write`,
`validator.py` and `scip_resolver.py` for any Load of a `Node` field outside the
contract. Without it, a field read that no test corpus happens to exercise would
be an AttributeError waiting in production rather than a failing test.
`index_repo` itself is deliberately excluded: it legitimately reads full `Node`
fields *before* the projection and on `late_nodes`.

**Item #2b: drop the in-RAM edge list — NO LONGER SKIPPED, now DONE.**
The original entry below concluded this was not worth doing because
`validate_graph` needed the full edge set right before the write regardless.
That premise is gone: `EdgeStats` (item #13) turned its two edge-side checks
into running tallies, and polymorphic dispatch (item #12) moved into the
database. Those were the last two full-edge-set consumers.

The resolve sink now retains **only** `_RETAINED_EDGE_TYPES` (as SlimEdge) plus
whatever is in `defer_edge_types` (as full Edge, because it isn't written yet).
Everything else is durable in Neo4j as of the enqueue and is dropped on the
floor. Retention is prevented rather than released late — the bulk never enters
`all_edges` at all, so this removes the peak instead of freeing it afterwards.

Measured on the Python corpus: of 1946 edges produced by resolve, **18 are
retained (0.9%) and 1928 dropped (99.1%)**. The retained set is bounded by
declarations (~2-3 per node), not call sites. Projected at the 130M-edge target,
retained bulk goes from ~8.3 GB (56 B object + 8 B list pointer) to ~0.

All reported counts now come from `edge_stats.total` rather than
`len(all_edges)`, which is only a partial list from here on.

**Consequence: `GRAPH_LOWRAM_DERIVE` is now not merely redundant but actively
harmful.** Its whole purpose was to spill the bulk to disk so derive could stream
it; with nothing left that reads the bulk, the spill is **write-only** — the only
remaining uses of the `DiskEdgeStore` in `_lowram_derive_and_write` are `len()`
and the `rmtree` that deletes it. Enabling the flag now writes 130M edges to disk
and deletes them unread, for no benefit the default path doesn't already give.
It should be deleted (with `lowram_derive.py`, the tail, the guards, the flag and
its tests); left standing only pending that call.

**Item #13 (new): validate_graph stopped scanning edges — DONE.**
`validate_graph(nodes, edges)` became `validate_graph(nodes, edge_stats)`. Its
only edge-side work was a per-type histogram and a dangling-endpoint count —
both pure accumulations, now tallied in `validator.EdgeStats` as edges are
produced (seeded after extraction, updated in the resolve sink and each derive
pass). Identical numbers, since it counts the same multiset the scan did;
verified against `result.edges` on both corpora. `add_count` folds in edges
created by Cypher that never pass through Python. SCIP is the one stage that
*replaces* already-counted edges, so it recounts via `reset()` — unreachable on
the default path.

**Two always-dead checks removed here**, both the same shape: a check reading a
field the pre-resolve write blanks in RAM, running after that write.
`"function nodes missing core metrics"` (`n.loc <= 0 or n.cyclomatic <= 0`)
fired for every function on every run; the `end_col < start_col` range check
never fired at all.

**Item #12 (new): polymorphic dispatch moved into the database — DONE.**
`_synthesize_polymorphic_calls` (normal path) and `streaming_polymorphic_calls`
(low-RAM) both deleted; `cypher_derive_reference.synthesize_polymorphic_calls_cypher`
is now wired into `index_repo`, running after the final edge write (it needs
every `CALLS` and `OVERRIDES` durable first). **This was the last derive pass
that scanned the whole CALLS bulk** — which is what unblocks item #2b.

Consumers are unaffected: the Cypher still sets `r.strategy='polymorphic_dispatch'`,
which is what `agents.py:339` and `taint.py:1081,1102` read off the Neo4j
relationship. Moving expansion to query time instead would have broken them.

**Two intentional divergences.** (a) The arbitrary top-25 fan-out cap
(`_POLY_FANOUT_GUARD`) is gone — it had no faithful Cypher equivalent because
its selection depended on Python list-append order, and uncapped is complete and
order-independent rather than arbitrary. On very wide hierarchies this creates
more edges than before. (b) `evidence_file` is no longer propagated to synthetic
edges; nothing read it. `evidence_line` still is (`exception_walk.py:220` and
`two_agent`'s `caller_evidence_lines`).

Validation now runs **after** the write rather than at the end of derive, so the
Cypher's output is included in the reported totals (`EdgeStats.add_count` folds
in the count the query returns). Nothing gates on `validation["ok"]` — it is
reported only, at `ingest/build.py:109` — so the move is safe.

**Coverage loss, stated plainly:** the synthetic edges can no longer be asserted
from the test suite, because they are produced by the database. The tests now
only verify that both paths *invoke* the Cypher. Neither corpus exercised it
meaningfully anyway (the Python corpus has no `OVERRIDES`; the Java one has 5
but no callers of the overridden methods), so this loses little in practice —
but a real-corpus check against Neo4j is the only thing that can confirm it now.

Also in this sweep: `SlimEdge` shrank to `(type, src, dst)` — 88 B → ~56 B per
retained stand-in, and the low-RAM spill shrank by the same fraction — because
the polymorphic pass was the **only** Python code that read `evidence_*` off an
existing edge. And three dead edge properties stopped being written
(`extractor`, `evidence_col`, `evidence_file`), taking the per-edge write payload
from 8 properties to 4.

**Item #11 (new): dead code and unpersisted properties — DONE.**
- The `if False:` joblib dump block (69 lines) plus `get_dump_graph_path` /
  `get_dump_shard_size`. It could never have been re-enabled as written: its own
  guard raised on `stream_nodes or stream_writer`, both hardcoded `True`.
- `IndexResult.roles` — the role pass went in item #3; every construction passed
  a hardcoded `{}`.
- 12 node properties stopped being persisted (still computed and used during the
  build where relevant): `package`, `start_col`, `end_col`, `display_name`,
  `param_types`, `host`, `loc`, `cyclomatic`, `branch_count`, `loop_count`,
  `role_source`, `extractor`. Node write payload: 30 → 18 properties.
  `modifiers` deliberately kept; the booleans (`is_abstract`/`is_static`/
  `is_async`) kept because `_clean` drops False so only the true ones cost
  anything. Decorators are unaffected — they are `ANNOTATED_WITH` edges to
  `Annotation` nodes, not a node property.

**Two always-dead checks found and removed while doing this**, both the same
shape — a check reading a field that the pre-resolve write blanks in RAM, run
after that write, so it could never fire:
- `validate_graph`'s "function nodes missing core metrics" warning
  (`n.loc <= 0 or n.cyclomatic <= 0`) fired for **every function on every run**.
- `validate_graph`'s `end_col < start_col` range check never fired at all.

**`arg_names` was deliberately NOT removed** (user's call, held for later).
Worth recording what was found: both extractors compute it
(`_pass_arg_names`/`_call_arg_names` walk every call site's argument list) and
then **never pass the result to `ref()`** — so `RawRef.arg_names` is always
empty and every `Edge.arg_names` is `None`. The extraction work is pure waste
today; wiring it up or dropping the computation are both open.

**Item #10 (new): removed the DFG pass and `dfg_json` entirely — DONE.**
Follows item #9: with `PASSES` gone, `dataflow.py`'s only remaining product was
the per-function summary serialized onto each `Function` node as `dfg_json`.

Unlike #8 and #9 this was **not** a zero-consumer removal — `dfg_json` had real
readers (`taint.py` at 5 Cypher sites / ~12 parse sites, `agents.py`'s
`_has_sink_flows`). It is a deliberate architectural swap, made on the user's
decision: Agent B's deterministic taint composition (`find_taint_findings`) is
retired in favour of Agent C reading the source of each candidate path directly.

The reasoning: `dfg_json` supplies two facts per call site — which of the
caller's params flow into an argument (`from_params`), and which callee param it
lands in (`arg_position`/`arg_keyword`). The second is what lets a *program*
thread a parameter index across hops; without it a deterministic walker has
nothing to carry and every callee must be treated as wholly tainted. But an LLM
reading the same source re-derives both more accurately than the heuristic does
(which explicitly gave up on `*args`/`**kwargs` splats). Once Agent C reads the
path anyway, `dfg_json` is a precomputed, lossier copy of its own conclusion.

Also decisive: the pass cost a **second full tree-sitter parse of the whole
repo** (it re-read every file with a Function node from disk, in its own
`ProcessPoolExecutor`), plus ~1.8 KB of JSON per function held on the node
objects from dataflow through to the final write (~1.8 GB at 1M functions). A
deterministic taint pass over a graph too expensive to build is worth nothing.

Removed: `graph_core/dataflow.py` (whole module), the `dfg_json`/
`dfg_hash`/`dfg_returns_from_params` fields on `Node` and their `props()`
entries, `IndexResult.dfg`, `_derived_semantics_rows()` and both
`store.write_semantics()` calls — with items #3/#8/#10 all landed, **no derive
pass sets a node property any more**, so there is nothing left to patch back
onto the already-written nodes. `store.write_semantics()` itself is kept as a
generic helper. `dataflow.py` was also dropped from `test_slim_edge.py`'s
`_SLIM_READERS` (it was the other whole-file edge reader).

Verified: node and edge counts are **unchanged** on both corpora (Python
692/3286, Java 41/90) — `dfg_json` was a node *property*, so the graph's shape
never depended on it; only `write_semantics` rows go 353 → 0. Low-RAM vs default
remains equivalent. The `deriving` stage drops from 1.34s to 0.00s on the Python
corpus and the test suite from ~7.2s to ~1.0s, both reflecting the removed
second parse.

**Fingerprint note:** `graph_fingerprint.py` still SELECTs `n.dfg_hash` (along
with the already-removed `component_role`/`fan_in`/`fan_out`/`module_id`) and
deliberately should — Cypher returns null for a missing property rather than
erroring, so old goldens stay comparable and the divergence is the expected one.

**Downstream break (intentional):** `taint.py`'s `find_taint_findings`,
`find_sanitizer_candidates`, `_own_sink_params` and `agents.py`'s
`_has_sink_flows` all read `dfg_json` and will get nulls until Agent C replaces
them. Reversing this is cheap if Agent C disappoints — the pass is one module
plus ~40 lines of wiring, recoverable from git.

**Item #9 (new): removed `PASSES` edges entirely — DONE.** Same zero-consumer
test that removed `fan_in`/`fan_out` in item #8, applied to the other thing
dataflow produced. A full grep of the `sail` monorepo (all 8 services, every
file type, plus untyped `-[r]->` traversals that could sweep it up implicitly)
found **zero** consumers: the analyzer's taint/agents passes traverse
`CALLS`/`READS`/`WRITES` and never `PASSES`, and the payload arrays
(`flow_from_param`/`flow_to_param`/`flow_lines`/`const_args`) were read by
nothing outside the builder. The only mentions anywhere were two markdown files
describing how the edges were *built*.

Removed: the ArgFlow→callee binding pass, `calls_by_src` and
`calls_conf_by_pair` (both existed **only** to build these edges — that is 2 of
the 3 remaining full scans of the edge set in derive), the PASSES `Edge`
construction, the 4 payload fields on `Edge`, `PASSES` from `schema.EDGE_TYPES`,
`defer_edge_types`' PASSES entry (with SCIP off nothing is deferred at all now),
and `_resolve_arg_position` plus the now-unused `Confidence`/`Origin` imports in
`dataflow.py`. The resolver's unreachable `ref.type in ("CALLS", "PASSES")` arm
was narrowed to `CALLS` (no extractor ever emitted a PASSES ref — verified).

**Deliberately kept: `dfg_json`.** That is dataflow's real product and it *is*
load-bearing — `taint.py` reads it at 5 Cypher sites and ~12 parse sites
(`_own_sink_params`, taint-source gating, the composition walk, the architecture
pass), and `agents.py::_has_sink_flows` uses it for span selection. So the
second full-repo parse that produces it stays. Note the binding never reached
`dfg_json` anyway: `summary.to_json()` runs *before* the binding assigned
`af.callee_id`, so the serialized summaries always carried an empty
`callee_id` — which is why the analyzer resolves callees from the graph's own
`CALLS` edges instead.

Measured cost of what was removed: 701 B per PASSES edge (4.6× a CALLS edge —
five payload lists), 1.36 PASSES per Function node, held as a **full** `Edge`
object (never slimmed, a deferred type) from dataflow through to the final
write — i.e. right through the derive→write peak. ~0.96 GB at 1M functions.

Verified with a before/after capture on both corpora: `dfg_json` is
**byte-identical** (353 rows, same node set, zero differing hashes on the Python
corpus; 13/13 on Java), every non-PASSES edge type has identical counts, and
node/semantics counts are unchanged. Python: 3767 → 3286 edges, exactly the 481
PASSES removed and nothing else. Java had no PASSES, so it is unchanged at 90.
Low-RAM vs default remains equivalent. Guarded by
`test_lowram_equivalence.py::test_passes_edges_are_not_produced`.

**Fingerprint note:** `graph_fingerprint.py` hashes every relationship type via
an untyped `-[r]->`, so the golden fingerprints **will** diverge on the PASSES
row. That is the expected isolated divergence for an intentional removal (same
as item #8) — confirm the only delta is the missing type, then rebaseline.

**Port note:** this repo is the sandbox copy. The same edits are needed in
`sail/services/developer_assistant/app/services/code_review/graph_engine/graph_core/`.
`test_corpora/python_sample/` is a frozen input fixture and was deliberately
left untouched.

**Item #5 (`GRAPH_LOWRAM_DERIVE` guard fix) — DONE.** Implemented as designed
below: the flag now *composes* with the unconditional streaming writer instead
of conflicting with it, and the guard rejects only its still-valid
restrictions (SCIP / incremental / disk-streamed refs). Three defects were
found and fixed in the process:

1. **Unreachable** — `stream_writer` became an unconditional `True` (item #2),
   so the old guard raised on every run. The flag had never actually executed
   since that change.
2. **Guard ran too late** — it sat just above `resolve()`, by which point
   `store.wipe()` had run, every extracted node had been written, and their
   bulky fields had been blanked in RAM. A rejected run left the repo
   half-ingested in Neo4j. The check now runs before extraction, with nothing
   written (covered by
   `test_lowram_equivalence.py::test_lowram_rejects_unsupported_combinations_before_writing`).
3. **Silently wrong derive** — the biggest one, and it raised nothing.
   `EXTENDS`/`IMPLEMENTS`/`ANNOTATED_WITH` are not emitted by the extractors;
   they are `RawRef`s that only become edges inside `resolve()`. The old code
   split edge types on the *pre-resolve* `all_edges` (where they never appear)
   and let the sink spill the real ones to disk, so `_derive_overrides` saw an
   empty class hierarchy → zero `OVERRIDES` → zero polymorphic-dispatch
   `CALLS`. The split now happens in the sink, on resolve's own output.
   `test_lowram_tail.py` could not have caught this — it hands the tail its
   structural edges directly — hence the new end-to-end
   `test_lowram_equivalence.py` over `test_corpora/java_sample` (the Python
   corpus emits no `EXTENDS` at all and does not exercise it).

Verified byte-equivalent to the normal path on both corpora: identical node
ids, edge keys, per-type edge counts, `write_semantics` rows, and node field
values. The write contract also changed — under the streaming writer the
pre-derive graph is already durable, so the tail now writes only `late_nodes`
+ `write_semantics` + newly derived edges, instead of re-writing all nodes
(which would have pushed the blanked fields over the good Neo4j rows) and
re-writing every spilled bulk edge.

The original sequencing note still applies to *using* it: it trades RAM for
real sequential disk I/O, so turn it on only when measured peak RSS demands
it. It is off by default and the default path is untouched.

Related fix in `dataflow.py`: `run_dataflow` built its `caller -> callees`
index over *every* `CALLS` edge in the graph, up front. That is an
edge-count-sized structure, which defeated `GRAPH_LOWRAM_DERIVE` entirely —
the path streams edges from disk precisely to avoid one. It is now built after
summarization and restricted to callers that actually produced an `ArgFlow`,
so it is bounded by summarized-functions-with-arguments rather than by total
`CALLS`. Same number of passes over the edge source, just relocated. Verified
identical output against the pre-change baseline on both corpora (Python
481 PASSES / 481 bound / 295 unbound; Java 0 / 0 / 3).

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

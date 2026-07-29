# Further RAM/cut opportunities — beyond MEMORY_ARCHITECTURE_PLAN.md

Additional ideas surfaced after the core plan (`MEMORY_ARCHITECTURE_PLAN.md`)
was finalized. None of these are implemented or folded into that plan yet —
this is a holding doc to review and decide on later. Ranked by impact vs.
effort/risk.

## 1. Expand the "clear these fields" list on Node (free, zero risk)

The core plan's item #1 only clears the 4 fields the existing code already
clears: `docstring`, `signature`, `param_types`, `return_type`. The earlier
field-by-field audit (done while designing the core plan) found more fields
that are equally proven dead weight — never read by anything downstream of
extraction — but never got added to that historical list:

`end_col`, `display_name`, `visibility`, `modifiers` (a `list[str]` — real
per-instance cost for heavily-annotated code), `is_static`, `is_abstract`,
`is_async`, `host`, `loc`, `cyclomatic`, `branch_count`, `loop_count`,
`is_lock`.

Same pattern as the existing 4: write full data to Neo4j early, then clear
these too. No new risk, no new mechanism — just a longer list in the same
already-planned clearing step.

## 2. Already-baked-in win worth knowing about (no new work needed)

`resolver.py::resolve()` already supports flushing its own produced edges to
Neo4j in ~10k-edge batches *during* resolution itself (the `edge_sink`
parameter, returning `SlimEdge` stand-ins in place of full edges) — not just
after `resolve()` returns. Today this is only wired when `stream_writer` is
on. Once the core plan makes `stream_writer` unconditional, this activates
automatically: resolve's own edge output never grows into one giant
unbounded in-RAM list even mid-resolve. **Resolve's peak edge-RAM drops too,
not just derive's** — this wasn't explicitly called out in the core plan's
description of item #2, worth knowing it's part of what's already covered.

## 3. Push the remaining edge-touching derive passes into Cypher (bigger lever, real complexity)

After the core plan, `_derive_overrides` and `_synthesize_polymorphic_calls`
are the *last* two passes still holding edges in Python RAM. Moving them to
Cypher (variable-length path matching for the override-hierarchy BFS,
similar for polymorphic dispatch) would get derive down to needing
**essentially zero edges in RAM at all** — the true end state for derive
memory.

This is exactly what the original team's own design doc scoped as an
"extreme-scale, opt-in fallback," not a default — it's real new Cypher logic,
not a flag flip:
- `_derive_overrides`: needs a variable-length `[:EXTENDS|IMPLEMENTS*1..N]`
  path match plus same-name+same-arity method matching — translatable, but
  the current Python BFS-with-multiple-inheritance-dedup logic needs careful
  equivalence checking.
- `_synthesize_polymorphic_calls`: needs the existing fan-out cap
  (`_POLY_FANOUT_GUARD = 25`) and dedup logic (`existing_into_children`)
  reproduced correctly in Cypher — the trickiest part to get byte-identical.

**Recommendation**: only pursue this if the measurement after the core plan
(see plan's "measure before deciding on GRAPH_LOWRAM_DERIVE" step) still
shows meaningful edge-RAM in derive. Premature otherwise — same
measure-first philosophy already applied to `GRAPH_LOWRAM_DERIVE`.

## 4. Scope `_derive_sql_links` to just Function+Table nodes (small, free)

Currently receives the full node list even though it only ever reads
`Function` and `Table` labeled nodes. Once nodes are already durable in
Neo4j (core plan item #1), querying just those two labels from Neo4j instead
of scanning the full in-RAM node list is a small, free trim — same spirit as
item #2b in the core plan (scope working sets to exactly what's needed).

## 5. Revisit ref-streaming once the core plan ships (optional, not required)

Disk-spilling (`GRAPH_STREAMING_INGEST`/`GRAPH_CHECKPOINT_ROOT`) for
resolve's ref list was found unreliable before — but that was in a world
where it was tangled up with `GRAPH_LOWRAM_DERIVE`'s hard mutual-exclusion
conflict. Once the core plan ships, that specific conflict is gone —
ref-streaming becomes a clean, independent, purely-optional lever for
resolve's ref list only, unrelated to anything derive-side. Not needed given
core-plan item #1 already helps resolve's RAM too, but the earlier bad
experience with it may not apply anymore post-refactor — worth a second look
if resolve RAM is ever still a concern after measuring.

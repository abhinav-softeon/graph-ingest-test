# Graph Ingest — Configuration Reference

Every setting is an environment variable read fresh by `graph_core/config.py`
(also settable via the Streamlit sidebar, CLI flags, or `apply_ingestion_toggles`).
This doc lists them all and gives the **least-resource** configuration.

---

## Neo4j connection

| Env var | Default | Notes |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Use `bolt://` (direct) for a single instance; `neo4j+s://…` for Aura. |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `testpassword` | Must match your DB or you get `AuthError`. |
| `NEO4J_DATABASE` | `neo4j` | |

## Ingestion mode

| Env var | Default | Effect on resources |
|---|---|---|
| `GRAPH_EXTRACT_BATCH_SIZE` | `2000` | Files read+parsed per batch. **Smaller = less extraction RAM**, more overhead. (A huge value ≈ "no chunking / one giant batch" — the OOM path.) |
| `GRAPH_EXTRACT_WORKERS` | CPU count | Parallel extraction processes. **More = faster but more RAM** (each worker holds a batch). Linux `fork` is copy-on-write (cheaper); macOS/Windows `spawn` copies. |
| `GRAPH_CHECKPOINT_ROOT` | *(empty)* | Set a path to enable disk-spill checkpointing of extraction batches (crash-resume + bounded extraction memory). Prereq for streaming. |
| `GRAPH_STREAMING_INGEST` | `false` | Resolves against a slim node projection + streams refs from disk during resolve → **cuts resolve RAM** (refs + node text). Needs `GRAPH_CHECKPOINT_ROOT`. |

**Node/edge early-write + slimming is unconditional, not a toggle.** Every run
writes extracted nodes to Neo4j before resolve and drops their bulky/unused
fields from RAM, and flushes edges to Neo4j as they reach final form keeping
only slim stand-ins in RAM — this used to be gated behind `GRAPH_STREAMING_WRITER`
(now removed) and disk-spill checkpointing for no structural reason; it's the
default, always-on behavior now (`MEMORY_ARCHITECTURE_PLAN.md` items #1/#2).

## Low-RAM derive (Option A) — currently non-functional, deferred

| Env var | Default | Effect |
|---|---|---|
| `GRAPH_LOWRAM_DERIVE` | `false` | **Spills the 100M+ bulk edges to disk** during resolve and streams them through derive, so they never sit in RAM. Trades **speed (extra disk I/O) for much lower RAM**. Requires SCIP **off** (guarded). |
| `GRAPH_EDGE_SPILL_DIR` | `.graph_edge_spill` | Where the on-disk edge shards go. **Put on fast local SSD/NVMe** with ~10 GB free for a 130M-edge graph. |

⚠️ **`GRAPH_LOWRAM_DERIVE=true` currently always raises `RuntimeError` immediately
instead of running.** Its guard (`pipeline.py`) was written when streaming-writer
was optional and mutually exclusive with it; now that node/edge early-write is
unconditional, the guard's condition is always true, making this path
permanently unreachable as written. Left unfixed deliberately — items #1/#2/#3/#6
of `MEMORY_ARCHITECTURE_PLAN.md` already remove most of what this path existed
to work around (derive no longer holds much in the way of edges regardless).
Fixing the guard (item #5) is deferred until a real-corpus RAM measurement on
top of those items shows it's still needed. Don't enable this until then.

## Concurrency

| Env var | Default | Effect on resources |
|---|---|---|
| `GRAPH_WRITE_WORKERS` | `1` | Concurrent Neo4j write batches. **More = faster writes but more concurrent transaction memory** on the Neo4j side (watch `dbms.memory.transaction.total.max`). |
| `GRAPH_WRITE_BATCH_SIZE` | `5000` | Rows per write transaction. **Smaller = less Neo4j transaction memory** (safer vs OOM), more commits; larger = fewer commits, heavier transactions. |
| `GRAPH_RESOLVE_WORKERS` | `1` | ⚠️ Experimental parallel resolve — **degrades resolution precision at chunk boundaries** (import-context split). Keep at `1`. |
| `GRAPH_CACHE_IO_WORKERS` | `16` | Extract-cache I/O threads (I/O-bound; cheap). |
| `GRAPH_ZIP_EXTRACT_WORKERS` | `min(16, cpu*2)` | Unzip threads. |

## Optional subsystems

| Env var | Default | Notes |
|---|---|---|
| `GRAPH_SCIP_ENABLED` | `false` | Precise SCIP resolution. Off = heuristic resolver (fast). Incompatible with low-RAM derive. |
| `GRAPH_EXTRACT_CACHE_ENABLED` | `true` | Content-hash extraction cache; **keep on** — makes re-runs far cheaper. |
| `GRAPH_EXTRACT_CACHE_DIR` | `.cache/graph_extract_cache` | |

### Compiled hot-path (resolve + derive)

`resolver.py` and `pipeline.py` (resolve + derive, ~80% of ingest wall time
per `runs/*.json`) can be compiled to native extensions with Cython instead
of running interpreted — same algorithm, same memory layout, just less
per-operation interpreter overhead. This is a **build-time** choice, not a
runtime env var: CPython always prefers a compiled extension over a
same-named `.py` file when both exist, so "on/off" means "was
`setup_cython.py` run for this environment or not."

- **Docker (recommended path — matches the Dockerfile's existing
  `build-essential`):** `docker build --build-arg ENABLE_COMPILED_HOTPATH=true .`
  Off by default (`false`) — building adds a Cython install + compile step to
  the image build, and the plain `.py` files always work as the fallback.
- **Local/no Docker:** needs a C compiler (MSVC on Windows, gcc/clang on
  Linux/macOS) — not always available. `pip install cython setuptools wheel
  && python setup_cython.py build_ext --inplace` from the repo root.
- **Checking what's actually active:** `graph_core.config.compiled_hotpath_status()`
  reports per-module (resolver/pipeline) whether the loaded module is the
  compiled extension or plain Python, by inspecting `__file__`. Surfaced as a
  read-only status checkbox in the Streamlit sidebar (Optional subsystems).

## Advanced / rarely changed

| Env var | Default | Notes |
|---|---|---|
| `GRAPH_RESOLVE_CHUNK` | `250000` | Resolve progress-report granularity. |
| `GRAPH_RESOLVE_CHECKPOINT_SECONDS` | `60` | Resolve-state checkpoint cadence (only when checkpointing on without streaming). |
| `GRAPH_INDEX_LOCK_STALE_SECONDS` | `1800` | How long a namespace lock is honored before it's reclaimable as stale. |
| `GRAPH_LOG_LEVEL` | `INFO` | |

## Disabled (kept for re-enabling)

| Env var | Notes |
|---|---|
| `GRAPH_DUMP_GRAPH_PATH` | Joblib dump of the resolved graph — **currently disabled** in `pipeline.py` (`if False:`). See `load_graph_to_neo4j.py`. |
| `GRAPH_DUMP_SHARD_SIZE` | Edges per dump shard (default 2,000,000) when the dump path is re-enabled. |

---

## ⭐ Least-resource configuration (for the 130M-edge graph on a small box)

Node/edge early-write + slimming (the biggest lever — nodes/edges no longer
accumulate in full form for the whole repo) is now **always on**, so it's not
part of this config anymore. `GRAPH_LOWRAM_DERIVE` is currently non-functional
(see above) — don't set it. The remaining levers all target extraction/resolve:

```dotenv
GRAPH_CHECKPOINT_ROOT=/data/graph_checkpoints  # enables disk-spill checkpointing
GRAPH_STREAMING_INGEST=true                # slim node projection + refs streamed from disk
GRAPH_SCIP_ENABLED=false

# --- keep transactions + extraction small ---
GRAPH_WRITE_BATCH_SIZE=5000
GRAPH_WRITE_WORKERS=1                       # sequential writes = least concurrent txn memory
GRAPH_EXTRACT_BATCH_SIZE=1000               # smaller batches = less extraction RAM
GRAPH_EXTRACT_WORKERS=2                     # fewer workers = less extraction RAM (slower)
GRAPH_RESOLVE_WORKERS=1                     # never raise (precision + memory)

# --- keep the cache on (cheap, speeds re-runs) ---
GRAPH_EXTRACT_CACHE_ENABLED=true
```

**Notes**
- Not yet re-measured on the real 130M-edge corpus since items #1/#2/#3/#6
  landed — the peak-RSS numbers above predate this architecture change. Treat
  this section as directionally right, not re-validated; re-measure before
  relying on it (`MEMORY_ARCHITECTURE_PLAN.md` item #17/final step).
- `GRAPH_EXTRACT_WORKERS=2` trades extraction speed for RAM. On Linux (`fork`,
  copy-on-write) you can raise it more cheaply than on macOS.

## For contrast — fastest / highest-resource

```dotenv
GRAPH_STREAMING_INGEST=false
GRAPH_WRITE_WORKERS=4                        # parallel writes (raise Neo4j txn memory too)
GRAPH_WRITE_BATCH_SIZE=20000
GRAPH_EXTRACT_WORKERS=                       # = CPU count
```

## Docker (hard 12 GB, no swap)

See `docker-compose.yml`: `mem_limit: 12g` + `memswap_limit: 12g` +
`mem_swappiness: 0` enforces a hard cap with no swap (OOM-kills instead of
swapping). It sets the least-resource config above and points at Neo4j on the
host via `bolt://host.docker.internal:7687`. Docker Desktop's VM must be given
> 12 GB for the cap to be enforceable.

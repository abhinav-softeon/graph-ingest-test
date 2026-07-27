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
| `GRAPH_STREAMING_INGEST` | `false` | Streams refs from disk + slim node projection during resolve → **cuts resolve RAM** (refs + node text). Needs `GRAPH_CHECKPOINT_ROOT`. |
| `GRAPH_STREAMING_WRITER` | `false` | Flushes edges to Neo4j during resolve, keeps slim stand-ins → **cuts resolve/derive RAM** further. Needs streaming ingest. |

## Low-RAM derive (Option A) — the big lever for huge graphs

| Env var | Default | Effect |
|---|---|---|
| `GRAPH_LOWRAM_DERIVE` | `false` | **Spills the 100M+ bulk edges to disk** during resolve and streams them through derive, so they never sit in RAM. Trades **speed (extra disk I/O) for much lower RAM**. Requires streaming ingest/writer **off** and SCIP **off** (guarded). |
| `GRAPH_EDGE_SPILL_DIR` | `.graph_edge_spill` | Where the on-disk edge shards go. **Put on fast local SSD/NVMe** with ~10 GB free for a 130M-edge graph. |

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

Prioritizes **completing without OOM** over speed. Expected peak ~5–7 GB
(estimate — validate with the live RAM readout). Slower (multiple disk passes).

```dotenv
# --- the RAM levers ---
GRAPH_LOWRAM_DERIVE=true
GRAPH_EDGE_SPILL_DIR=/data/edge_spill      # fast SSD, ~10GB free
GRAPH_STREAMING_INGEST=false               # required off with low-RAM derive (guarded)
GRAPH_STREAMING_WRITER=false               # required off (guarded)
GRAPH_SCIP_ENABLED=false                   # required off (guarded)

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
- The remaining RAM after this is nodes + refs + resolve indices (all scale with
  node/ref count, not edge count). If the measured resolve peak is still tight,
  the next lever is the **streaming-ingest + low-RAM combo** (not yet wired) —
  it moves refs + node text off RAM during resolve.
- `GRAPH_EXTRACT_WORKERS=2` trades extraction speed for RAM. On Linux (`fork`,
  copy-on-write) you can raise it more cheaply than on macOS.

## For contrast — fastest / highest-resource

```dotenv
GRAPH_LOWRAM_DERIVE=false                   # everything in RAM (needs ~26GB+ for 130M edges)
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

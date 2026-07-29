"""Runtime configuration for the vendored graph engine.

Infrastructure shim: unlike the original graph_rag config, this does NOT load a
`.env` file. Neo4j connection values come from the service environment (set by
`app/core/config.py` / docker-compose), so the graph engine shares one config
source with the rest of developer_assistant. SCIP indexers are optional and
default to disabled unless a binary is explicitly available.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field


def scip_python_bin() -> str | None:
    """Locate the scip-python indexer binary, or None to fall back to the
    heuristic resolver. Order: $SCIP_PYTHON_BIN -> PATH -> None.

    SCIP is optional in the service (precise Python resolution). Per-PR indexing
    stays fast on the heuristic resolver when no binary is present."""
    env = os.environ.get("SCIP_PYTHON_BIN")
    if env:
        for cand in (env, env + ".cmd", env + ".ps1"):
            if os.path.exists(cand):
                return cand
    return shutil.which("scip-python")


def scip_java_bin() -> str | None:
    """Locate the scip-java indexer binary, or None. Order: $SCIP_JAVA_BIN ->
    PATH -> None (heuristic resolver fallback)."""
    env = os.environ.get("SCIP_JAVA_BIN")
    if env:
        for cand in (env, env + ".bat", env + ".cmd"):
            if os.path.exists(cand):
                return cand
    return shutil.which("scip-java")


def scip_java_max_files() -> int:
    """Repos with more Java files than this skip scip-java entirely and stay
    on the heuristic resolver. scip-java compiles the whole project to get
    type info, so on a very large monorepo that cost may not be worth the
    precision gain. 0 (default) means no limit."""
    try:
        return int(os.environ.get("SCIP_JAVA_MAX_FILES", "0"))
    except ValueError:
        return 0


def extract_worker_count() -> int:
    """Process-pool size for parallel file extraction (CPU-bound tree-sitter
    parsing, one file fully independent of another). Defaults to CPU count.
    Uses processes, not threads: each language's tree-sitter Parser is a
    module-level singleton (see languages.py), not safe to call concurrently
    from multiple threads — a fresh process gets its own parser instances."""
    env = os.environ.get("GRAPH_EXTRACT_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return max(1, os.cpu_count() or 1)


def extract_batch_size() -> int:
    """Number of files discovered (read + hashed) and extracted per batch.

    Chunks discover()+extraction so at most one batch's worth of raw file
    content is held in memory at a time, instead of every file in the repo
    simultaneously — the actual peak-memory driver for very large repos.
    Only affects the discover/extract memory lifecycle, not extraction or
    resolution logic/results."""
    env = os.environ.get("GRAPH_EXTRACT_BATCH_SIZE")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return 2000


def checkpoint_root() -> str | None:
    """Directory for per-batch extraction checkpoints (joblib-serialized).

    Must be a persistent (non-tmpfs) path shared across worker restarts —
    checkpoints exist specifically to survive a crash/OOM/restart, so tmpfs
    would defeat the purpose. Returns None (checkpointing disabled) if unset,
    so this is opt-in via GRAPH_CHECKPOINT_ROOT rather than assumed.
    """
    root = os.environ.get("GRAPH_CHECKPOINT_ROOT", "").strip()
    return root or None


def is_streaming_ingest_enabled() -> bool:
    """Switch for resolving against a slim (id/name/fqn-only) node projection
    instead of full Node objects, and — when checkpointing is also enabled
    (GRAPH_CHECKPOINT_ROOT) — streaming refs from disk one batch at a time
    instead of holding the whole ref list in RAM (see TIER3_MEMORY_PLAN.md).
    Node/edge early-write and slimming themselves are unconditional now
    (MEMORY_ARCHITECTURE_PLAN.md items #1/#2) and do not depend on this flag.
    Default OFF. Opt in with GRAPH_STREAMING_INGEST=1/true."""
    env = os.environ.get("GRAPH_STREAMING_INGEST", "").strip().lower()
    return env in ("1", "true", "yes", "on")


def is_streaming_writer_enabled() -> bool:
    """Historical GRAPH_STREAMING_WRITER flag. The behavior it used to gate —
    eager edge flush to Neo4j with only SlimEdge stand-ins retained in RAM —
    is now unconditional in pipeline.py (MEMORY_ARCHITECTURE_PLAN.md item #2),
    so this function is no longer read by the pipeline. Kept only because
    ingest/build.py still reports GRAPH_STREAMING_WRITER's env value in its
    diagnostic status dict; the env var itself no longer has any effect on
    ingestion behavior."""
    env = os.environ.get("GRAPH_STREAMING_WRITER", "").strip().lower()
    return env in ("1", "true", "yes", "on")


def resolve_checkpoint_seconds() -> float:
    """How often resolve() persists its progress checkpoint (seconds).

    Under the streaming writer this checkpoint is metadata-only and each save
    forces a durability barrier (queued edge writes are drained first), so the
    interval trades crash-resume granularity against that sync cost. Default 60s.
    Settable mainly so tests can force the checkpoint path on a small corpus."""
    env = os.environ.get("GRAPH_RESOLVE_CHECKPOINT_SECONDS")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    return 60.0


def resolve_chunk_size() -> int:
    """Refs resolved and flushed to Neo4j per chunk under streaming ingest.
    Bounds the ref/edge working set independent of total ref count."""
    env = os.environ.get("GRAPH_RESOLVE_CHUNK")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return 250_000


def is_extract_cache_enabled() -> bool:
    """Whether extracted graph data should be cached in S3 to skip
    re-extraction across runs. Defaults to enabled; opt out with
    GRAPH_EXTRACT_CACHE_ENABLED=0/false/no."""
    env = os.environ.get("GRAPH_EXTRACT_CACHE_ENABLED", "true").strip().lower()
    return env not in ("0", "false", "no", "")


def get_extract_cache_dir() -> str:
    """Local filesystem directory for the extraction cache. Standalone app has
    no S3 dependency — replaces the original get_extract_cache_prefix()."""
    return os.environ.get("GRAPH_EXTRACT_CACHE_DIR", "").strip() or os.path.join(".cache", "graph_extract_cache")


def compiled_hotpath_status() -> dict[str, bool]:
    """Whether resolver.py/pipeline.py are running as compiled Cython
    extensions (built via setup_cython.py, see its docstring) rather than
    interpreted Python. Not a runtime switch — compiling is a build-time
    step (a native C compiler is required; this project's Dockerfile already
    has build-essential) — this only *detects and reports* what's actually
    loaded, by checking whether each module's __file__ is an extension
    (.so/.pyd) or a plain .py source file. CPython's import machinery always
    prefers a compiled extension over a same-named .py file when both are
    present, so the only real "toggle" is whether setup_cython.py was run
    for a given environment."""
    import graph_core.pipeline as _pipeline
    import graph_core.resolver as _resolver

    def _is_compiled(mod) -> bool:
        f = getattr(mod, "__file__", "") or ""
        return f.endswith((".so", ".pyd"))

    return {
        "resolver": _is_compiled(_resolver),
        "pipeline": _is_compiled(_pipeline),
    }


def is_scip_enabled() -> bool:
    """Master ON/OFF checkbox for SCIP precise resolution, independent of
    whether scip-python/scip-java binaries are actually found (see
    scip_python_bin()/scip_java_bin() above, which no-op gracefully either
    way). Default off; opt in with GRAPH_SCIP_ENABLED=1/true."""
    env = os.environ.get("GRAPH_SCIP_ENABLED", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def get_cache_io_workers() -> int:
    """Thread-pool size for extract-cache GET/PUT I/O (was a hardcoded
    constant, _CACHE_IO_WORKERS=16, in pipeline.py) — configurable so it can
    be experimented with alongside extract_worker_count()."""
    env = os.environ.get("GRAPH_CACHE_IO_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return 16


def get_zip_extract_workers() -> int:
    """Thread-pool size for extracting files out of an uploaded zip (was a
    hardcoded formula, min(16, cpu*2), in upload_utils.py)."""
    env = os.environ.get("GRAPH_ZIP_EXTRACT_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return min(16, max(1, (os.cpu_count() or 4) * 2))


def get_resolve_workers() -> int:
    """Process-pool size for the experimental parallel resolve() path (see
    resolver_parallel.py). 1 (default) = today's untouched sequential
    resolve() in resolver.py. >1 opts into the new parallel path."""
    env = os.environ.get("GRAPH_RESOLVE_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return 1


def get_write_workers() -> int:
    """Thread-pool size for concurrent Neo4j write batches (store.py).
    1 (default) = today's untouched sequential write. >1 opts into
    concurrent batches — threads, not processes, since a write batch is
    I/O-bound (waiting on the Neo4j server), not CPU-bound, so this carries
    none of the fork/spawn overhead extraction/resolve workers do."""
    env = os.environ.get("GRAPH_WRITE_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return 1


def get_lock_stale_seconds() -> int:
    """How long (seconds) a graph index lock may be held before it's
    considered stale and eligible to be reclaimed by another worker."""
    try:
        return int(os.environ.get("GRAPH_INDEX_LOCK_STALE_SECONDS", "1800"))
    except ValueError:
        return 1800


def get_lowram_derive() -> bool:
    """Opt-in low-RAM derive path (GRAPH_LOWRAM_DERIVE). When on, the pre-derive
    edge set is spilled to disk (DiskEdgeStore) and the derive passes stream it
    instead of holding all edges (100M+) in RAM. Off by default — the proven
    in-RAM path is unchanged. See graph_core/lowram_derive.py.

    NOTE: since the streaming writer (stream_writer) became unconditional
    (MEMORY_ARCHITECTURE_PLAN.md item #2), pipeline.py's own guard —
    `if lowram: if stream_writer or is_streaming_ingest_enabled(): raise
    RuntimeError(...)` — makes this path unreachable: setting
    GRAPH_LOWRAM_DERIVE=1 now always raises immediately instead of running.
    Left as-is deliberately (item #5, deferred until real-corpus RAM is
    measured against items #1/#2/#3/#6) — fix the guard only if measurement
    shows this safety valve is still needed."""
    return os.environ.get("GRAPH_LOWRAM_DERIVE", "").strip().lower() in ("1", "true", "yes", "on")


def get_edge_spill_dir() -> str:
    """Directory for the low-RAM derive edge spill (GRAPH_EDGE_SPILL_DIR).
    Default ./.graph_edge_spill. Only used when GRAPH_LOWRAM_DERIVE is on."""
    return os.environ.get("GRAPH_EDGE_SPILL_DIR", "").strip() or ".graph_edge_spill"


def get_write_batch_size() -> int:
    """Rows per Neo4j write transaction (store.py write_nodes/write_edges/
    write_semantics). Smaller = less transaction memory (safer against
    dbms.memory.transaction.total.max, esp. with >1 write worker) but more
    round-trips; larger = fewer commits but heavier transactions. Default 5000."""
    try:
        n = int(os.environ.get("GRAPH_WRITE_BATCH_SIZE", "5000"))
        return n if n > 0 else 5000
    except ValueError:
        return 5000


def get_dump_shard_size() -> int:
    """Edges per shard when dumping the resolved graph (GRAPH_DUMP_SHARD_SIZE).
    The dump is written — and read back by load_graph_to_neo4j.py — one shard at
    a time, so the loader never holds all edges in RAM simultaneously. Matters for
    very large graphs (100M+ edges), where a single-blob load would OOM. Default
    2,000,000."""
    try:
        n = int(os.environ.get("GRAPH_DUMP_SHARD_SIZE", "2000000"))
        return n if n > 0 else 2_000_000
    except ValueError:
        return 2_000_000


def get_dump_graph_path() -> str:
    """If set (GRAPH_DUMP_GRAPH_PATH), index_repo dumps the fully
    resolved+derived (nodes, edges) to this joblib path and SKIPS the Neo4j
    write, so the expensive extract/resolve/derive is done once and the graph
    can be loaded into Neo4j separately (see load_graph_to_neo4j.py). Requires
    streaming ingest/writer OFF (they hold no single final graph in memory)."""
    return os.environ.get("GRAPH_DUMP_GRAPH_PATH", "").strip()


@dataclass(frozen=True)
class Neo4jConfig:
    """Neo4j connection, read from the service environment at construction time.

    Fields are read lazily via default_factory so the process can set/override
    NEO4J_* env vars (docker-compose, app.core.config) before a review builds a
    GraphStore, without this dataclass capturing values at import time."""
    uri: str = field(default_factory=lambda: os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    user: str = field(default_factory=lambda: os.environ.get("NEO4J_USER", "neo4j"))
    password: str = field(default_factory=lambda: os.environ.get("NEO4J_PASSWORD", "testpassword"))
    database: str = field(default_factory=lambda: os.environ.get("NEO4J_DATABASE", "neo4j"))


def neo4j_config() -> Neo4jConfig:
    return Neo4jConfig()

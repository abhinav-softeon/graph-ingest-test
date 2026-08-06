"""Runtime configuration for the vendored graph engine.

Infrastructure shim: unlike the original graph_rag config, this does NOT load a
`.env` file. Neo4j connection values come from the service environment (set by
`app/core/config.py` / docker-compose), so the graph engine shares one config
source with the rest of developer_assistant.

SCIP was removed entirely. scip-java indexes by compiling the project through
Maven/Gradle, and the ingested tree is source-only — the upload path strips
build files, and the target repos may have none at all — so it could never run.
graph_core/javac_resolver.py replaces it: javac resolves in-repo types from
`-sourcepath` alone, needing no build system.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def is_javac_resolver_enabled() -> bool:
    """Use javac to resolve Java CALLS instead of heuristic name matching.

    Measured against javac ground truth on a 16.5k-file Java repo, the heuristic
    scored 93.8% recall but 5.0% precision — it finds the right target and then
    emits every same-named candidate beside it, having no type system to choose
    with. Ambiguous CALLS alone measured 0.1% precision. javac has the types.

    Default off; opt in with GRAPH_JAVAC_RESOLVER=1/true. Falls back to the
    heuristic on any failure (no JDK, compile abort, timeout, thin coverage)."""
    env = os.environ.get("GRAPH_JAVAC_RESOLVER", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def javac_timeout_seconds() -> float:
    """Wall-clock budget for the javac attribution pass (default 3600).

    Unlike a Maven build this is pure attribution — no dependency resolution,
    no artifact download — but a large monorepo still takes real time. On
    timeout the pass is abandoned and Java stays on the heuristic resolver."""
    try:
        v = float(os.environ.get("GRAPH_JAVAC_TIMEOUT_SECONDS", "3600"))
    except ValueError:
        return 3600.0
    return v if v > 0 else 3600.0


def javac_batch_size() -> int:
    """Files per javac task (default 400). Attribution holds the whole batch's
    symbol table, so this is the memory knob: lower it if javac OOMs, rather
    than raising the heap."""
    try:
        v = int(os.environ.get("GRAPH_JAVAC_BATCH_SIZE", "400"))
    except ValueError:
        return 400
    return v if v > 0 else 400


def is_bytecode_resolver_enabled() -> bool:
    """Read Java CALLS/READS/WRITES out of compiled bytecode.

    Strictly better than javac where class files exist: javac re-derives the
    bindings, bytecode simply carries them. It also supplies what no
    source-level pass can — lambda bodies, anonymous inner classes and static
    initializers as real methods (HANDOFF 4.2), and field access with the exact
    owning class rather than only explicit `this.x` (HANDOFF 4.4).

    Default off; opt in with GRAPH_BYTECODE_RESOLVER=1/true. Coverage is
    per-file, so files without class files stay on javac or the heuristic."""
    env = os.environ.get("GRAPH_BYTECODE_RESOLVER", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def bytecode_class_roots() -> list[str]:
    """Explicit class/jar locations, os.pathsep-separated.

    Normally left unset: the pass discovers .class directories and archives via
    discovery.discover_artifacts. Set GRAPH_BYTECODE_CLASS_ROOTS when the
    compiled output lives outside the uploaded tree."""
    raw = os.environ.get("GRAPH_BYTECODE_CLASS_ROOTS", "").strip()
    return [p for p in raw.split(os.pathsep) if p.strip()] if raw else []


def name_match_max_candidates() -> int:
    """Cap on how many candidates a BARE-NAME match may fan out to (default 5).

    A bare name matching N same-named declarations produces N edges of which one
    at most is correct — (N-1)/N false by construction. Above this cap the ref is
    recorded as unresolved instead, trading a small recall loss inside the
    lowest-trust tier for a large precision gain.

    Only strategy `name*` is capped — no scope, no import, no receiver type, the
    ~5%-precision bucket. same_scope/same_file/imports/receiver_type matches are
    never capped no matter how many candidates they have.

    GRAPH_NAME_MATCH_MAX_CANDIDATES=0 disables the cap entirely."""
    try:
        v = int(os.environ.get("GRAPH_NAME_MATCH_MAX_CANDIDATES", "5"))
    except ValueError:
        return 5
    return v if v >= 0 else 5


def javac_skip_above_bytecode_coverage() -> float:
    """Skip the javac pass when bytecode already covered this fraction of Java.

    Both tiers answer the same question — javac RE-DERIVES by recompiling what a
    class file already records — and pipeline already drops every javac edge whose
    file bytecode covered. So at high bytecode coverage javac's whole output is
    discarded after being paid for: one measured run compiled for 153 s, produced
    338,010 edges, and landed exactly 0 of them in the graph.

    This gates the WORK, not the edges. The files above the threshold that
    bytecode missed fall back to the heuristic resolver rather than javac, so the
    trade is a small precision loss on that remainder against the whole javac
    stage. Default 0.98: at the measured 16,673/16,677 (99.98%) it fires and 4
    files change tier.

    Set to 1.0 to skip only on total coverage, or 0 to disable the skip and
    always run javac."""
    try:
        v = float(os.environ.get("GRAPH_JAVAC_SKIP_ABOVE_BYTECODE_COVERAGE", "0.98"))
    except ValueError:
        return 0.98
    return min(max(v, 0.0), 1.0)


def polymorphic_dispatch_enabled() -> bool:
    """Materialize caller -> every-override CALLS edges in the database.

    Default OFF. Measured at 2,593,900 edges — 53.6% of one repo's entire CALLS
    set — every one of them AMBIGUOUS/DERIVED, and the pass is deliberately
    uncapped so a wide hierarchy multiplies without bound.

    Off costs nothing as long as consumers do the expansion at query time, which
    is cheaper and equally complete because OVERRIDES is already in the graph:

        MATCH (caller)-[:CALLS {strategy:'bytecode'}]->(m:Function)
        OPTIONAL MATCH (impl:Function)-[:OVERRIDES]->(m)
        RETURN caller, m, collect(impl) AS possible_impls

    Turn it back on only if something needs the edges pre-materialized — note
    that any consumer filtering to strategy='bytecode' is already excluding
    them, so for those the stored rows are pure cost.

    GRAPH_POLYMORPHIC_DISPATCH=1/true to enable."""
    env = os.environ.get("GRAPH_POLYMORPHIC_DISPATCH", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def external_all_calls() -> bool:
    """Emit CALLS_EXTERNAL for EVERY out-of-repo call, not just database work.

    Off by default. With it off, a call the classifier does not recognise is
    dropped (`external_calls` counts it and nothing is written) — which is
    correct for `inputStream.close()` but also silently loses every non-database
    sink: Runtime.exec, FileOutputStream, ObjectInputStream.readObject,
    response.getWriter().write. Those are invisible to any analysis seeded from
    sinks, and an LLM never sees them because nothing routes it to the function.

    With it on, unrecognised targets classify as external_api.EXTERNAL_OTHER.
    The External NODE count stays small (shared, keyed owner#method); the EDGE
    count does not — most of a repo's invocations leave the repo. Enable it when
    you want completeness over volume, or extend the classifier instead.

    GRAPH_EXTERNAL_ALL_CALLS=1/true."""
    env = os.environ.get("GRAPH_EXTERNAL_ALL_CALLS", "false").strip().lower()
    return env in ("1", "true", "yes", "on")


def bytecode_min_match_rate() -> float:
    """Quality floor for the bytecode pass (default 0.5).

    This is the STALE BUILD guard, and it is the failure mode that matters:
    class files from an old build parse perfectly and produce confident edges
    for code that no longer exists. If fewer than this fraction of bytecode
    methods match a source node, the bytecode does not describe this source
    tree and the whole pass is discarded rather than trusted."""
    try:
        v = float(os.environ.get("GRAPH_BYTECODE_MIN_MATCH_RATE", "0.5"))
    except ValueError:
        return 0.5
    return v if 0.0 < v <= 1.0 else 0.5


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
    """Streams refs from disk one batch at a time during resolve, instead of
    holding the whole ref list in RAM, when checkpointing is also enabled
    (GRAPH_CHECKPOINT_ROOT). At ~150 B per RawRef and multiple million refs on a
    large repo, that list is one of the few things left that scales with call
    sites rather than declarations — so this is now the main lever it controls.

    It used to ALSO gate resolving against a slim node projection. That is
    unconditional now: `all_nodes` is itself projected to SlimNode right after
    the pre-resolve write and resolve reads it directly, so there is no longer a
    separate projection to switch on (MEMORY_ARCHITECTURE_PLAN.md item #14).
    Node/edge early-write and slimming are likewise unconditional (items #1/#2).

    Default OFF. Opt in with GRAPH_STREAMING_INGEST=1/true."""
    env = os.environ.get("GRAPH_STREAMING_INGEST", "").strip().lower()
    return env in ("1", "true", "yes", "on")


# is_streaming_writer_enabled() (GRAPH_STREAMING_WRITER) lived here. The behaviour it
# gated is unconditional (item #2); it survived only to echo the env var back in a
# diagnostic dict, which is worse than not reporting it at all.


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


# resolve_chunk_size() (GRAPH_RESOLVE_CHUNK) lived here — nothing ever read it.


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


# get_resolve_workers() (GRAPH_RESOLVE_WORKERS) lived here, selecting a parallel
# resolve path that the streaming writer made unreachable. Removed with it (item #18).


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


# get_lowram_derive() / get_edge_spill_dir() lived here, backing
# GRAPH_LOWRAM_DERIVE and GRAPH_EDGE_SPILL_DIR. The path they gated spilled the
# bulk edges to disk so derive could stream them back; once nothing read the
# bulk any more the spill was write-only, so the flag and its whole code path
# were removed (MEMORY_ARCHITECTURE_PLAN.md item #16). The default path now
# retains only structural edges, which is what the flag existed to achieve.


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


# get_dump_shard_size() / get_dump_graph_path() lived here, backing the
# GRAPH_DUMP_SHARD_SIZE / GRAPH_DUMP_GRAPH_PATH dump-the-whole-graph escape
# hatch in index_repo. That block was already disabled and un-re-enablable
# (it needed the whole graph in RAM, which the unconditional streaming write
# rules out); both were removed together.


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

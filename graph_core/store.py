"""Neo4j writer: schema bootstrap + batched node/edge upserts.

Every node gets the shared label :CodeNode (single unique id index) plus its
specific label. Labels and rel-types are validated against the schema allowlist
before being interpolated into Cypher (Cypher can't parametrize them).
"""
from __future__ import annotations

import itertools
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, Iterator, Optional

from neo4j import GraphDatabase

from .config import Neo4jConfig, get_write_batch_size, get_write_workers
from .models import Edge, Node
from .schema import DROPPED_EDGE_TYPES, SHARED_LABEL, assert_edge, assert_label
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


class GraphStore:
    def __init__(self, cfg: Neo4jConfig):
        self._cfg = cfg
        self._driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))

    def close(self):
        self._driver.close()

    def _run(self, query, **params):
        with self._driver.session(database=self._cfg.database) as s:
            return s.run(query, **params).consume()

    def read(self, query, **params) -> list[dict]:
        """Run a read query and materialize the rows as plain dicts."""
        with self._driver.session(database=self._cfg.database) as s:
            return [r.data() for r in s.run(query, **params)]

    def bootstrap(self):
        _log.info("[graph_store] bootstrap: asserting constraints")
        self._run(
            f"CREATE CONSTRAINT code_node_id IF NOT EXISTS "
            f"FOR (n:{SHARED_LABEL}) REQUIRE n.id IS UNIQUE"
        )
        # Per-codebase graph metadata / lock lives on GraphMeta nodes; assert its
        # uniqueness constraint here too so it's set up alongside the code graph.
        self.bootstrap_graph_meta()

    def bootstrap_graph_meta(self) -> None:
        """Assert the GraphMeta uniqueness constraint (idempotent).

        Mirrors bootstrap(): one namespace per GraphMeta node. Safe to call
        independently of bootstrap()."""
        self._run(
            "CREATE CONSTRAINT graph_meta_namespace IF NOT EXISTS "
            "FOR (g:GraphMeta) REQUIRE g.namespace IS UNIQUE"
        )

    def get_graph_meta(self, namespace: str) -> dict | None:
        rows = self.read(
            "MATCH (g:GraphMeta {namespace:$namespace}) RETURN properties(g) AS props",
            namespace=namespace,
        )
        return rows[0]["props"] if rows else None

    def upsert_graph_meta(self, namespace: str, fields: dict) -> None:
        now = time.time()
        # Never let caller-supplied fields clobber the namespace identity.
        safe = {k: v for k, v in (fields or {}).items() if k != "namespace"}
        self._run(
            "MERGE (g:GraphMeta {namespace:$namespace}) "
            "ON CREATE SET g.created_at = $now "
            "SET g += $fields, g.updated_at = $now",
            namespace=namespace,
            fields=safe,
            now=now,
        )

    def list_graph_meta(self) -> list[dict]:
        rows = self.read("MATCH (g:GraphMeta) RETURN properties(g) AS props")
        return [r["props"] for r in rows]

    def delete_graph_meta(self, namespace: str) -> None:
        # Removes only the metadata node, never the code graph for the namespace.
        self._run(
            "MATCH (g:GraphMeta {namespace:$namespace}) DETACH DELETE g",
            namespace=namespace,
        )

    def create_vector_index(self, dim: int, name: str = "code_embedding"):
        """Native Neo4j vector index over :CodeNode(embedding), cosine similarity.
        `dim` is interpolated (not parametrizable in index OPTIONS) but is an int
        we control — the model's dimension — so there is no injection surface."""
        d = int(dim)
        self._run(
            f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
            f"FOR (n:{SHARED_LABEL}) ON (n.embedding) "
            f"OPTIONS {{indexConfig: {{"
            f"`vector.dimensions`: {d}, `vector.similarity_function`: 'cosine'}}}}"
        )

    def wipe(self, repo: str):
        # Use batched delete to avoid large single-transaction memory spikes on
        # very large namespaces (e.g., big upload/full reviews).
        _log.info("[graph_store][repo=%s] wipe: deleting entire namespace", repo)
        t0 = time.time()
        try:
            self._run(
                f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) "
                f"CALL {{ WITH n DETACH DELETE n }} IN TRANSACTIONS OF 1000 ROWS",
                repo=repo,
            )
        except Exception:
            # Fallback for environments where IN TRANSACTIONS may be restricted.
            self._run(f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) DETACH DELETE n", repo=repo)
        _log.info("[graph_store][repo=%s] wipe: done in %.3fs", repo, time.time() - t0)

    def _write_in_batches(
        self,
        batches: Iterator[tuple[str, list[dict]]],
        total: int,
        on_batch: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """Run a sequence of (query, rows) batches, each in its own managed
        write transaction, returning the total rows written.

        ``batches`` is a lazy iterator (not a pre-built list) so at most
        ``get_write_workers()`` batches' worth of prop-dicts exist in memory
        at once — same discipline as the old strictly-sequential code, just
        bounded by worker count instead of always being exactly 1.

        Sequential (workers<=1, the default): identical to the old
        behavior — one ``session.run`` at a time via ``self._run``.

        Parallel (workers>1): each batch runs via ``session.execute_write``
        (a managed transaction), which auto-retries on Neo4j's transient
        deadlock error — necessary because concurrent batches (especially
        edges, which can share endpoint nodes across types/batches) can
        legitimately deadlock; raw ``session.run`` never retries.
        ``on_batch`` is only ever invoked from this method's own thread
        (never from inside a worker), matching zip-extraction's fix earlier:
        it may end up calling into Streamlit, which requires the main
        thread.
        """
        write_workers = get_write_workers()
        written = 0

        if write_workers <= 1:
            for q, rows in batches:
                self._run(q, rows=rows)
                written += len(rows)
                if on_batch:
                    on_batch(written, total)
            return written

        def _run_one(q: str, rows: list[dict]) -> int:
            with self._driver.session(database=self._cfg.database) as s:
                s.execute_write(lambda tx: tx.run(q, rows=rows).consume())
            return len(rows)

        with ThreadPoolExecutor(max_workers=write_workers) as pool:
            in_flight: dict = {}
            for q, rows in itertools.islice(batches, write_workers):
                in_flight[pool.submit(_run_one, q, rows)] = None
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    written += fut.result()
                    del in_flight[fut]
                    if on_batch:
                        on_batch(written, total)
                    nxt = next(batches, None)
                    if nxt is not None:
                        q, rows = nxt
                        in_flight[pool.submit(_run_one, q, rows)] = None
        return written

    def write_nodes(self, nodes: list[Node], on_batch=None):
        """``on_batch(written, total)``: optional per-batch callback. A
        multi-hundred-thousand-node write is one of the longest operations in
        the pipeline and used to be completely silent — no logs, and (fatally)
        no job heartbeat, so the stale-job janitor could requeue a perfectly
        healthy job mid-write. Callers pass this to keep the heartbeat alive."""
        ts = int(time.time())
        # Bucket by label only (references to the existing Node objects, not
        # copies) — building every node's Neo4j-ready props dict up front,
        # for the whole graph, would duplicate all of it (once as Node
        # objects — still held by the caller — once as plain dicts) right at
        # the point overall memory is already at its peak for the whole
        # pipeline. _write_in_batches's lazy generator converts one _BATCH
        # slice at a time (times get_write_workers() in flight), not all at once.
        by_label: dict[str, list[Node]] = defaultdict(list)
        for n in nodes:
            by_label[assert_label(n.label)].append(n)
        t0 = time.time()
        batch = get_write_batch_size()

        def _batches() -> Iterable[tuple[str, list[dict]]]:
            for label, label_nodes in by_label.items():
                # last_indexed comes from row.ts (not a top-level $ts param) —
                # each (query, rows) batch is self-contained since
                # _write_in_batches only ever passes a single `rows` param.
                q = (
                    f"UNWIND $rows AS row "
                    f"MERGE (n:{SHARED_LABEL} {{id: row.id}}) "
                    f"SET n:{label}, n += row.props, n.last_indexed = row.ts"
                )
                for i in range(0, len(label_nodes), batch):
                    rows = [{"id": n.id, "props": n.props(), "ts": ts} for n in label_nodes[i:i + batch]]
                    yield q, rows

        written = self._write_in_batches(iter(_batches()), len(nodes), on_batch)
        _log.info(
            "[graph_store] write_nodes: wrote %s node(s) across %s label(s) in %.3fs (%s)",
            written, len(by_label), time.time() - t0,
            ", ".join(f"{lbl}={len(ns)}" for lbl, ns in by_label.items()),
        )

    def write_edges(self, edges: list[Edge], on_batch=None):
        # Same reasoning as write_nodes: bucket by type only (references, not
        # copies), convert to props dicts lazily via _write_in_batches.
        # ``on_batch(written, total)``: see write_nodes — keeps the job
        # heartbeat alive through a long write.
        # THE choke point for schema.DROPPED_EDGE_TYPES. Filtering here rather
        # than at each extractor catches every producer, including edges built
        # inside resolve() (the REFERENCES fallback) and the derive passes, so no
        # emission site can leak one back in. See schema.DROPPED_EDGE_TYPES.
        by_type: dict[str, list[Edge]] = defaultdict(list)
        dropped = 0
        for e in edges:
            if e.type in DROPPED_EDGE_TYPES:
                dropped += 1
                continue
            by_type[assert_edge(e.type)].append(e)
        if dropped:
            _log.debug("[graph_store] write_edges: skipped %s edge(s) of dropped type(s)", dropped)
        t0 = time.time()
        batch = get_write_batch_size()

        def _batches() -> Iterable[tuple[str, list[dict]]]:
            for rtype, type_edges in by_type.items():
                q = (
                    f"UNWIND $rows AS row "
                    f"MATCH (a:{SHARED_LABEL} {{id: row.src}}) "
                    f"MATCH (b:{SHARED_LABEL} {{id: row.dst}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) "
                    f"SET r += row.props"
                )
                for i in range(0, len(type_edges), batch):
                    rows = [
                        {"src": e.src, "dst": e.dst, "props": e.props()}
                        for e in type_edges[i:i + batch]
                    ]
                    yield q, rows

        written = self._write_in_batches(iter(_batches()), len(edges), on_batch)
        type_counts = {k: len(v) for k, v in by_type.items()}
        _log.info(
            "[graph_store] write_edges: wrote %s edge(s) across %s type(s) in %.3fs (%s)",
            written, len(type_counts), time.time() - t0,
            ", ".join(f"{t}={c}" for t, c in type_counts.items()),
        )

    def write_semantics(self, rows: list[dict]):
        """Patch semantic properties onto existing nodes by id.

        Each row is {"id": ..., "props": {...}}. This is a targeted SET (not a
        full node rewrite) so the deterministic structural props are untouched —
        the semantic layer only annotates nodes the parser already wrote.
        """
        q = (
            f"UNWIND $rows AS row "
            f"MATCH (n:{SHARED_LABEL} {{id: row.id}}) "
            f"SET n += row.props"
        )
        t0 = time.time()
        batch = get_write_batch_size()

        def _batches() -> Iterable[tuple[str, list[dict]]]:
            for i in range(0, len(rows), batch):
                yield q, rows[i:i + batch]

        self._write_in_batches(iter(_batches()), len(rows))
        _log.info("[graph_store] write_semantics: patched %s node(s) in %.3fs", len(rows), time.time() - t0)

    def file_shas(self, namespace: str) -> dict[str, str]:
        """Return {relpath: sha} for every File node in the namespace.

        The content sha is written on File nodes as `body_hash` (see the
        extractors' File Node(...) construction — every extractor sets
        body_hash=file.sha for the File node specifically); tolerate its
        absence (older graphs) and skip any node missing file or sha."""
        rows = self.read(
            f"MATCH (n:File:{SHARED_LABEL} {{repo:$namespace}}) "
            f"RETURN n.file AS file, n.body_hash AS sha",
            namespace=namespace,
        )
        out: dict[str, str] = {}
        for r in rows:
            f, sha = r.get("file"), r.get("sha")
            if f and sha:
                out[f] = sha
        return out

    def delete_files(self, namespace: str, relpaths: list[str]) -> None:
        """DETACH DELETE every :CodeNode in the namespace belonging to one of the
        given files. No-op on an empty list; the changeset is bounded so a single
        statement is sufficient (cf. wipe(), which batches whole namespaces)."""
        if not relpaths:
            return
        _log.info("[graph_store][repo=%s] delete_files: removing nodes for %s file(s)", namespace, len(relpaths))
        t0 = time.time()
        self._run(
            f"MATCH (n:{SHARED_LABEL} {{repo:$namespace}}) "
            f"WHERE n.file IN $relpaths "
            f"DETACH DELETE n",
            namespace=namespace,
            relpaths=relpaths,
        )
        _log.info("[graph_store][repo=%s] delete_files: done in %.3fs", namespace, time.time() - t0)

    def counts(self, namespace: str) -> tuple[int, int]:
        """(node_count, edge_count) for the namespace.

        Edges carry no repo property (see Edge.props()), so relationships are
        counted only when both endpoints are :CodeNode nodes in the namespace."""
        with self._driver.session(database=self._cfg.database) as s:
            nodes = s.run(
                f"MATCH (n:{SHARED_LABEL} {{repo:$namespace}}) RETURN count(n) AS c",
                namespace=namespace,
            ).single()["c"]
            rels = s.run(
                f"MATCH (a:{SHARED_LABEL} {{repo:$namespace}})-[r]->"
                f"(b:{SHARED_LABEL} {{repo:$namespace}}) RETURN count(r) AS c",
                namespace=namespace,
            ).single()["c"]
        return nodes, rels

    def acquire_lock(self, namespace: str, token: str, stale_before: float, job_id: str | None = None) -> bool:
        """Atomically claim the per-namespace lock in one Cypher statement.

        The MERGE runs inside a write transaction so concurrent claims serialize;
        we only win if a row comes back carrying our own token.

        ``job_id`` (optional) is stored alongside the lock so a process other
        than the one holding it — the janitor — can tell whose lock this is
        and release it as soon as that job is already terminal in Postgres,
        instead of only being able to wait out the blind staleness timeout.
        That timeout is the only recourse for a lock left by a process that
        was hard-killed (OOM/SIGKILL) — no in-process cleanup (``finally``)
        can run in that case, by construction."""
        now = time.time()
        q = (
            "MERGE (g:GraphMeta {namespace:$namespace}) "
            "ON CREATE SET g.created_at = $now "
            "WITH g "
            "WHERE g.locked_at IS NULL OR g.locked_at < $stale_before "
            "SET g.locked_at = $now, g.lock_token = $token, g.lock_job_id = $job_id "
            "RETURN g.lock_token AS token"
        )
        with self._driver.session(database=self._cfg.database) as s:
            rows = s.execute_write(
                lambda tx: list(
                    tx.run(
                        q,
                        namespace=namespace,
                        token=token,
                        now=now,
                        stale_before=stale_before,
                        job_id=job_id,
                    )
                )
            )
        acquired = bool(rows) and rows[0]["token"] == token
        _log.info("[graph_store][repo=%s] acquire_lock: %s (token=%s, job_id=%s)", namespace, "acquired" if acquired else "already held by another run", token, job_id)
        return acquired

    def release_lock(self, namespace: str, token: str) -> None:
        """Release the lock only if we still hold it (token match)."""
        self._run(
            "MATCH (g:GraphMeta {namespace:$namespace}) "
            "WHERE g.lock_token = $token "
            "SET g.locked_at = NULL, g.lock_token = NULL, g.lock_job_id = NULL",
            namespace=namespace,
            token=token,
        )
        _log.info("[graph_store][repo=%s] release_lock: released (token=%s)", namespace, token)

    def force_release_lock(self, namespace: str) -> bool:
        """Clear the per-namespace lock regardless of token — for recovering from
        a hard-killed job (Ctrl-C / OOM / `docker stop`) whose release_lock
        finally-block never ran, so the lock would otherwise sit stuck until the
        stale timeout. Clears only the lock fields; GraphMeta identity/status/
        codebase_hash are untouched.

        ONLY use when you are sure no build for this namespace is actually
        running — force-unlocking a live job lets a second writer start
        concurrently. Returns True if a GraphMeta node was found (whether or not
        it was locked)."""
        rows = self.read(
            "MATCH (g:GraphMeta {namespace:$namespace}) "
            "SET g.locked_at = NULL, g.lock_token = NULL, g.lock_job_id = NULL "
            "RETURN 1 AS ok",
            namespace=namespace,
        )
        _log.info("[graph_store][repo=%s] force_release_lock: cleared lock (existed=%s)", namespace, bool(rows))
        return bool(rows)

"""Insert a resolved-graph dump (from `cli.py --dump-graph` / the UI's
"Build → dump" tab) into Neo4j.

The dump is a streamed, sharded pickle: a header frame, then a nodes frame, then
N edge-shard frames. ``insert_dump`` reads it frame-by-frame and writes each shard
to Neo4j before reading the next, so all edges are never resident at once — this
is what makes a 100M+ edge graph loadable on a modest box (and into AuraDB).

The expensive work (extract/resolve/derive) already happened at dump time, so this
does ONLY the Neo4j write: the fast, I/O-bound part. Re-runnable and
connection-overridable, so the same dump loads into local Neo4j or AuraDB and the
batch size / write workers can be retuned without ever redoing resolve.

Examples:
    python load_graph_to_neo4j.py --dump graph.joblib --wipe
    python load_graph_to_neo4j.py --dump graph.joblib \
        --neo4j-uri neo4j+s://xxxx.databases.neo4j.io \
        --neo4j-user neo4j --neo4j-password '***' --write-workers 4
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph_core.config import neo4j_config
from graph_core.store import GraphStore


def read_dump_header(path: str) -> dict:
    """Read just the header frame (cheap) — counts, shard layout, repo."""
    with open(path, "rb") as fh:
        header = pickle.load(fh)
    if not (isinstance(header, dict) and header.get("format") == "sharded-v1"):
        raise ValueError(f"{path} is not a sharded-v1 graph dump (header={header!r})")
    return header


def insert_dump(
    store: GraphStore,
    path: str,
    wipe: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Stream a sharded dump into Neo4j, one edge shard at a time.

    ``progress(text)`` is an optional status callback (never touches Neo4j).
    Returns a summary dict with repo, node/edge counts, and the DB's counts.
    """
    def _say(text: str) -> None:
        if progress:
            progress(text)

    with open(path, "rb") as fh:
        header = pickle.load(fh)
        if not (isinstance(header, dict) and header.get("format") == "sharded-v1"):
            raise ValueError(f"{path} is not a sharded-v1 graph dump (header={header!r})")
        repo = header["repo"]
        n_edges = header["n_edges"]
        n_shards = header["n_shards"]

        store.bootstrap()
        if wipe:
            _say(f"wiping namespace {repo}…")
            store.wipe(repo)

        nodes = pickle.load(fh)
        _say(f"writing {len(nodes)} node(s)…")
        store.write_nodes(nodes, on_batch=lambda w, t: _say(f"nodes {w}/{t}"))
        del nodes

        written = 0
        for s in range(n_shards):
            edges = pickle.load(fh)
            store.write_edges(
                edges,
                on_batch=lambda w, t, s=s, base=written: _say(
                    f"edges shard {s + 1}/{n_shards}: {base + w}/{n_edges}"
                ),
            )
            written += len(edges)
            del edges

    return {"repo": repo, "n_nodes": header["n_nodes"], "n_edges": n_edges, "counts": store.counts(repo)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Insert a resolved-graph dump into Neo4j.")
    p.add_argument("--dump", required=True, help="Path to the dump produced by --dump-graph")
    p.add_argument("--wipe", action="store_true", help="DETACH DELETE the repo namespace before inserting")
    p.add_argument("--write-workers", type=int, default=None, help="Concurrent write batches (default: env GRAPH_WRITE_WORKERS or 1)")
    p.add_argument("--write-batch-size", type=int, default=None, help="Rows per write transaction (default: env GRAPH_WRITE_BATCH_SIZE or 5000)")
    p.add_argument("--neo4j-uri", default=None)
    p.add_argument("--neo4j-user", default=None)
    p.add_argument("--neo4j-password", default=None)
    p.add_argument("--neo4j-database", default=None)
    args = p.parse_args(argv)

    if args.neo4j_uri:
        os.environ["NEO4J_URI"] = args.neo4j_uri
    if args.neo4j_user:
        os.environ["NEO4J_USER"] = args.neo4j_user
    if args.neo4j_password:
        os.environ["NEO4J_PASSWORD"] = args.neo4j_password
    if args.neo4j_database:
        os.environ["NEO4J_DATABASE"] = args.neo4j_database
    if args.write_workers:
        os.environ["GRAPH_WRITE_WORKERS"] = str(args.write_workers)
    if args.write_batch_size:
        os.environ["GRAPH_WRITE_BATCH_SIZE"] = str(args.write_batch_size)

    header = read_dump_header(args.dump)
    print(f"Loading {args.dump}: {header['n_nodes']} node(s), {header['n_edges']} edge(s), "
          f"{header['n_shards']} shard(s) (repo={header['repo']})")

    store = GraphStore(neo4j_config())
    try:
        t0 = time.time()
        summary = insert_dump(
            store, args.dump, wipe=args.wipe,
            progress=lambda text: print(f"  {text}", end="\r", flush=True),
        )
        print()
        print(f"Inserted into Neo4j in {time.time() - t0:.1f}s")
        print(f"counts (nodes, rels): {summary['counts']}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

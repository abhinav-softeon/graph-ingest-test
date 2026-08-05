from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from neo4j import GraphDatabase

from graph_core.config import neo4j_config
from graph_core.schema import SHARED_LABEL

_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BATCH = 1000


def _chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _safe_token(token: str, kind: str) -> str:
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ValueError(f"invalid {kind}: {token!r}")
    return token


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload root must be an object")
    if "nodes" not in payload or "edges" not in payload:
        raise ValueError("payload must contain 'nodes' and 'edges'")
    return payload


def _wipe_repo(session, repo: str) -> None:
    try:
        session.run(
            f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) "
            "CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS",
            repo=repo,
        ).consume()
    except Exception:
        session.run(
            f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) DETACH DELETE n",
            repo=repo,
        ).consume()


def _write_nodes(session, repo: str, nodes: list[dict[str, Any]]) -> int:
    by_label_set: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        node_id = n.get("id")
        if not node_id:
            continue
        labels = tuple(sorted(_safe_token(l, "label") for l in (n.get("labels") or [])))
        props = dict(n.get("props") or {})
        props["id"] = node_id
        props["repo"] = repo
        by_label_set[labels].append({"id": node_id, "props": props})

    total = 0
    for labels, rows in by_label_set.items():
        label_suffix = "".join(f":{label}" for label in labels)
        if label_suffix:
            query = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{SHARED_LABEL} {{id: row.id}}) "
                f"SET n{label_suffix}, n += row.props"
            )
        else:
            query = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{SHARED_LABEL} {{id: row.id}}) "
                f"SET n += row.props"
            )
        for batch in _chunks(rows, _BATCH):
            session.run(query, rows=batch).consume()
            total += len(batch)
    return total


def _write_edges(session, repo: str, edges: list[dict[str, Any]]) -> int:
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        src = e.get("src")
        dst = e.get("dst")
        rtype = e.get("type")
        if not src or not dst or not rtype:
            continue
        safe_type = _safe_token(rtype, "relationship type")
        by_type[safe_type].append(
            {"src": src, "dst": dst, "props": dict(e.get("props") or {})}
        )

    total = 0
    for rtype, rows in by_type.items():
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (a:{SHARED_LABEL} {{id: row.src, repo: $repo}}) "
            f"MATCH (b:{SHARED_LABEL} {{id: row.dst, repo: $repo}}) "
            f"MERGE (a)-[r:{rtype}]->(b) "
            f"SET r += row.props"
        )
        for batch in _chunks(rows, _BATCH):
            session.run(query, rows=batch, repo=repo).consume()
            total += len(batch)
    return total


def _upsert_graph_meta(session, repo: str, graph_meta: dict[str, Any]) -> None:
    props = dict(graph_meta or {})
    props.pop("namespace", None)
    session.run(
        "MERGE (g:GraphMeta {namespace:$repo}) "
        "SET g += $props",
        repo=repo,
        props=props,
    ).consume()


def import_graph(json_path: Path, repo_override: str | None, wipe_first: bool) -> tuple[str, int, int]:
    payload = _load_payload(json_path)
    repo = repo_override or payload.get("repo")
    if not repo:
        raise ValueError("repo not found in JSON; pass --repo")

    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    graph_meta = payload.get("graph_meta")

    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))
    try:
        with driver.session(database=cfg.database) as session:
            if wipe_first:
                _wipe_repo(session, repo)
            written_nodes = _write_nodes(session, repo, nodes)
            written_edges = _write_edges(session, repo, edges)
            if isinstance(graph_meta, dict):
                _upsert_graph_meta(session, repo, graph_meta)
    finally:
        driver.close()

    return repo, written_nodes, written_edges


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a graph JSON file back into Neo4j (namespace-scoped)."
    )
    parser.add_argument("--in", dest="in_path", required=True, help="Input JSON file path")
    parser.add_argument(
        "--repo",
        default=None,
        help="Override namespace from JSON (optional)",
    )
    parser.add_argument(
        "--wipe-first",
        action="store_true",
        help="Delete existing nodes for the namespace before import",
    )
    args = parser.parse_args()

    repo, n_nodes, n_edges = import_graph(
        json_path=Path(args.in_path),
        repo_override=args.repo,
        wipe_first=args.wipe_first,
    )
    print(f"Imported repo={repo} nodes={n_nodes} edges={n_edges} from {args.in_path}")


if __name__ == "__main__":
    main()

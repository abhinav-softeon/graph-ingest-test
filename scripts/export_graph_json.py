from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from graph_core.config import neo4j_config
from graph_core.schema import SHARED_LABEL


def _jsonify(value: Any) -> Any:
    """Recursively normalize Neo4j/python values into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, dt_time, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    return str(value)


def _read_nodes(session, repo: str) -> list[dict[str, Any]]:
    rows = session.run(
        f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) "
        "RETURN labels(n) AS labels, properties(n) AS props",
        repo=repo,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        labels = [l for l in (row["labels"] or []) if l != SHARED_LABEL]
        props = _jsonify(dict(row["props"] or {}))
        node_id = props.get("id")
        if not node_id:
            continue
        out.append({"id": node_id, "labels": sorted(labels), "props": props})
    return out


def _read_edges(session, repo: str) -> list[dict[str, Any]]:
    rows = session.run(
        f"MATCH (a:{SHARED_LABEL} {{repo:$repo}})-[r]->(b:{SHARED_LABEL} {{repo:$repo}}) "
        "RETURN a.id AS src, b.id AS dst, type(r) AS type, properties(r) AS props",
        repo=repo,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        src = row["src"]
        dst = row["dst"]
        rtype = row["type"]
        if not src or not dst or not rtype:
            continue
        out.append(
            {
                "src": src,
                "dst": dst,
                "type": rtype,
                "props": _jsonify(dict(row["props"] or {})),
            }
        )
    return out


def _read_graph_meta(session, repo: str) -> dict[str, Any] | None:
    row = session.run(
        "MATCH (g:GraphMeta {namespace:$repo}) RETURN properties(g) AS props",
        repo=repo,
    ).single()
    if not row:
        return None
    return _jsonify(dict(row["props"] or {}))


def export_graph(repo: str, out_path: Path, include_meta: bool) -> dict[str, Any]:
    cfg = neo4j_config()
    driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))
    try:
        with driver.session(database=cfg.database) as session:
            nodes = _read_nodes(session, repo)
            edges = _read_edges(session, repo)
            graph_meta = _read_graph_meta(session, repo) if include_meta else None
    finally:
        driver.close()

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": int(time.time()),
        "repo": repo,
        "database": cfg.database,
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
    }
    if include_meta and graph_meta is not None:
        payload["graph_meta"] = graph_meta

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export one Neo4j graph namespace to JSON (nodes + edges + optional GraphMeta)."
    )
    parser.add_argument("--repo", required=True, help="Graph namespace (repo tag)")
    parser.add_argument("--out", required=True, help="Output JSON file path")
    parser.add_argument(
        "--no-meta",
        action="store_true",
        help="Do not include GraphMeta in the export",
    )
    args = parser.parse_args()

    payload = export_graph(
        repo=args.repo,
        out_path=Path(args.out),
        include_meta=not args.no_meta,
    )
    print(
        f"Exported repo={payload['repo']} nodes={payload['counts']['nodes']} "
        f"edges={payload['counts']['edges']} -> {args.out}"
    )


if __name__ == "__main__":
    main()

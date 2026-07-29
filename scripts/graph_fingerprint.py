"""Phase 0 regression oracle for the graph-ingestion memory rework
(see MEMORY_ARCHITECTURE_PLAN.md).

Ported from the sister developer_assistant repo
(app/services/code_review/graph_engine/scripts/graph_fingerprint.py) — same
tool, same purpose, just with the `app.services.code_review.graph_engine.`
import prefix stripped since graph_core is top-level in this sandbox.

Captures a deterministic *fingerprint* of the resolved graph a run produces —
node/edge counts, an order-independent hash of the full edge set, resolver
Coverage, and a hash of derived node properties — so that every phase of the
streaming/slim-index rework can be proven to produce **byte-identical output**
to today's in-memory pipeline. If the fingerprint matches, resolution quality
did not change; if it diverges, the change regressed and must be blocked.

The fingerprint is read back from Neo4j (the final graph state), not from the
pipeline's in-memory structures, so it's independent of *how* the graph was
built — exactly the property a regression oracle needs.

Hashes are order-independent (a running sum of per-row digests) so they can be
computed by streaming rows from Neo4j without sorting or holding the whole
result set in the harness — the harness must not itself OOM on the graphs this
project is trying to make ingestable.

Usage:
    # Capture golden output from `main` (run the real pipeline, then fingerprint):
    python -m scripts.graph_fingerprint /path/to/repo --repo bench --out golden.json

    # Fingerprint an already-indexed namespace without re-running ingestion:
    python -m scripts.graph_fingerprint --repo bench --no-index --out candidate.json

    # Compare two fingerprints (exit code 1 on any divergence):
    python -m scripts.graph_fingerprint --compare golden.json candidate.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

from neo4j import GraphDatabase

from graph_core.config import Neo4jConfig
from graph_core.pipeline import index_repo
from graph_core.store import GraphStore

_SHARED_LABEL = "CodeNode"
_MOD = 1 << 128  # 128-bit accumulator for the order-independent row hashes


def _row_digest(*parts: object) -> int:
    """Stable 128-bit int digest of a row's fields. `None` and missing values
    normalize to '' so an absent property and an empty one fingerprint the same
    (Node.props() already drops empties before writing to Neo4j)."""
    s = "\x1f".join("" if p is None else str(p) for p in parts)
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest(), "big")


def _on_stage(stage: str, detail: dict) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {stage:<16} {detail}")


def fingerprint(cfg: Neo4jConfig, repo: str, coverage: dict | None,
                seconds: float | None, stage_seconds: dict | None) -> dict:
    """Read the final graph state for `repo` from Neo4j into a fingerprint dict.

    Streams the big result sets (edges, node props) so harness memory stays flat
    regardless of graph size."""
    driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))
    try:
        with driver.session(database=cfg.database) as s:
            # Counts — aggregated inside Neo4j, cheap.
            node_counts = {
                r["label"]: r["c"]
                for r in s.run(
                    f"MATCH (n:{_SHARED_LABEL} {{repo:$repo}}) "
                    f"UNWIND labels(n) AS l WITH l WHERE l <> '{_SHARED_LABEL}' "
                    f"RETURN l AS label, count(*) AS c",
                    repo=repo,
                )
            }
            edge_counts = {
                r["t"]: r["c"]
                for r in s.run(
                    f"MATCH (a:{_SHARED_LABEL} {{repo:$repo}})-[r]->"
                    f"(b:{_SHARED_LABEL} {{repo:$repo}}) "
                    f"RETURN type(r) AS t, count(r) AS c",
                    repo=repo,
                )
            }

            # Edge-set hash — order-independent sum of per-edge digests over the
            # identity + resolution-decision fields (type, endpoints, strategy,
            # confidence). Streamed, no sort, O(1) harness memory.
            edge_hash = 0
            edge_total = 0
            for r in s.run(
                f"MATCH (a:{_SHARED_LABEL} {{repo:$repo}})-[r]->"
                f"(b:{_SHARED_LABEL} {{repo:$repo}}) "
                f"RETURN type(r) AS t, a.id AS src, b.id AS dst, "
                f"r.strategy AS strat, r.confidence AS conf",
                repo=repo,
            ):
                edge_hash = (edge_hash + _row_digest(
                    r["t"], r["src"], r["dst"], r["strat"], r["conf"])) % _MOD
                edge_total += 1

            # Derived-property hash — the fields the derive passes write back
            # (role classification, call metrics, module ownership, dfg hash).
            derived_hash = 0
            node_total = 0
            for r in s.run(
                f"MATCH (n:{_SHARED_LABEL} {{repo:$repo}}) "
                f"RETURN n.id AS id, n.component_role AS role, n.fan_in AS fi, "
                f"n.fan_out AS fo, n.module_id AS mid, n.dfg_hash AS dh",
                repo=repo,
            ):
                derived_hash = (derived_hash + _row_digest(
                    r["id"], r["role"], r["fi"], r["fo"], r["mid"], r["dh"])) % _MOD
                node_total += 1
    finally:
        driver.close()

    return {
        "repo": repo,
        "node_count_total": node_total,
        "node_counts_by_label": dict(sorted(node_counts.items())),
        "edge_count_total": edge_total,
        "edge_counts_by_type": dict(sorted(edge_counts.items())),
        "edge_set_hash": f"{edge_hash:032x}",
        "derived_props_hash": f"{derived_hash:032x}",
        # Resolver quality counts, straight from IndexResult (None if --no-index).
        "coverage": {
            rt: {
                "total": c.total, "resolved": c.resolved, "ambiguous": c.ambiguous,
                "unresolved": c.unresolved, "external": c.external,
            }
            for rt, c in sorted((coverage or {}).items())
        },
        # Timing is recorded for the speed-regression check but is NOT part of
        # correctness — compare() ignores it.
        "_timing": {"seconds": seconds, "stage_seconds": stage_seconds},
    }


# Keys that define correctness. Timing (_timing) is deliberately excluded.
_CORRECTNESS_KEYS = [
    "node_count_total", "node_counts_by_label",
    "edge_count_total", "edge_counts_by_type",
    "edge_set_hash", "derived_props_hash", "coverage",
]


def compare(golden: dict, candidate: dict) -> list[str]:
    """Return a list of human-readable divergences (empty = identical output)."""
    diffs: list[str] = []
    for key in _CORRECTNESS_KEYS:
        g, c = golden.get(key), candidate.get(key)
        if g == c:
            continue
        if isinstance(g, dict) and isinstance(c, dict):
            for k in sorted(set(g) | set(c)):
                if g.get(k) != c.get(k):
                    diffs.append(f"{key}[{k}]: golden={g.get(k)} candidate={c.get(k)}")
        else:
            diffs.append(f"{key}: golden={g} candidate={c}")
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="Repo root to index (omit with --no-index)")
    parser.add_argument("--repo", default="bench", help="Graph repo tag (Neo4j namespace)")
    parser.add_argument("--no-scip", action="store_true", help="Heuristic resolver only")
    parser.add_argument("--no-wipe", action="store_true", help="Don't wipe the namespace first")
    parser.add_argument("--no-index", action="store_true",
                        help="Skip ingestion; fingerprint the existing namespace only")
    parser.add_argument("--out", help="Write the fingerprint JSON to this file")
    parser.add_argument("--compare", nargs=2, metavar=("GOLDEN", "CANDIDATE"),
                        help="Compare two fingerprint files and exit 1 on any divergence")
    args = parser.parse_args()

    if args.compare:
        with open(args.compare[0]) as fh:
            golden = json.load(fh)
        with open(args.compare[1]) as fh:
            candidate = json.load(fh)
        diffs = compare(golden, candidate)
        if diffs:
            print("REGRESSION — output differs:")
            for d in diffs:
                print(f"  - {d}")
            sys.exit(1)
        print("IDENTICAL — no resolution-quality change.")
        gt = (golden.get("_timing") or {}).get("seconds")
        ct = (candidate.get("_timing") or {}).get("seconds")
        if gt and ct:
            print(f"timing: golden={gt:.1f}s candidate={ct:.1f}s ({ct / gt:.2f}x)")
        return

    cfg = Neo4jConfig()
    coverage = seconds = stage_seconds = None
    if not args.no_index:
        if not args.path:
            parser.error("path is required unless --no-index is given")
        store = GraphStore(cfg)
        result = index_repo(
            args.path, args.repo, store,
            wipe=not args.no_wipe, scip=not args.no_scip, on_stage=_on_stage,
        )
        store.close()
        coverage, seconds, stage_seconds = result.coverage, result.seconds, result.stage_seconds

    fp = fingerprint(cfg, args.repo, coverage, seconds, stage_seconds)
    text = json.dumps(fp, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote fingerprint -> {args.out}")
    print(text)


if __name__ == "__main__":
    main()

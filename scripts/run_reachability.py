"""Run reachability marking on an already-built graph, without a full review.

    python scripts/run_reachability.py --repo experiment

WHY THIS EXISTS
`from_entry` / `reaches_sink` are ANALYSIS marks, written only by
reach.mark_all() — which runs inside a review (analysis/pipeline.py), not during
a graph build. So a freshly built graph legitimately reports a universe of 0:
the ingest-time marks (taint_source / taint_categories) are there, and nothing
has walked them yet.

This runs just that step, so reachability can be checked and tuned without
paying for a whole LLM review.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import reach  # noqa: E402
from graph_core.store import GraphStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--drop-db-other", action="store_true",
                    help="exclude db_other from the sink seed set. Try this if "
                         "the universe comes back near 100%%: ResultSet and "
                         "PreparedStatement member access is everywhere, so "
                         "seeding from db_other can mark half the repo and the "
                         "pruning stops pruning.")
    a = ap.parse_args()

    kinds = list(reach.DANGEROUS_KINDS)
    if a.drop_db_other:
        kinds = [k for k in kinds if k != "db_other"]
    print(f"sink kinds: {kinds}\n")

    store = GraphStore()
    try:
        pre = store.read(
            """
            MATCH (f:Function {repo: $repo})
            RETURN count(f) AS total,
                   sum(CASE WHEN f.taint_source = true THEN 1 ELSE 0 END) AS ingest_sources,
                   sum(CASE WHEN f.taint_categories IS NOT NULL
                                 AND size(f.taint_categories) > 0
                            THEN 1 ELSE 0 END) AS ingest_sinks
            """,
            repo=a.repo,
        )
        print("ingest-time marks (written by the BUILD):")
        print(" ", dict(pre[0]) if pre else "none")
        if pre and not (pre[0]["ingest_sources"] or pre[0]["ingest_sinks"]):
            print("\n  Both are 0 — the build wrote no taint marks. Usually stale\n"
                  "  pickled Node objects in the extract cache. Clear and rebuild:\n"
                  "    rm -rf .cache/graph_extract_cache .graph_checkpoints")
            return

        print("\nrunning reach.mark_all() ...\n")
        out = reach.mark_all(store, a.repo, kinds)
        print(json.dumps(out, indent=2))

        u = out.get("universe", {})
        frac = u.get("universe_fraction", 0)
        print()
        if not u.get("both"):
            side = ("no entry points" if not u.get("from_entry")
                    else "no sink-reaching functions" if not u.get("reaches_sink")
                    else "the two sets do not intersect")
            print(f"  UNIVERSE IS 0 — {side}.")
        elif frac > 0.5:
            print(f"  Universe is {frac:.1%} of the repo — the filter is barely "
                  f"filtering.\n  Re-run with --drop-db-other.")
        else:
            print(f"  Universe: {u['both']:,} of {u['total']:,} functions "
                  f"({frac:.1%}). That is the set Pass B enumerates paths through.")
    finally:
        store.close()


if __name__ == "__main__":
    main()

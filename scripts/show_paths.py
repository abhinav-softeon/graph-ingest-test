"""Enumerate entry->sink paths from the marked graph. No LLM.

    python scripts/show_paths.py --repo experiment
    python scripts/show_paths.py --repo experiment --kinds db_execute --show 15

The step after reachability: reach.mark_all() decides WHICH functions can lie on
a source->sink path; this walks the actual paths inside that subgraph. Still
fully deterministic — the LLM only enters afterwards, to judge whether a path is
genuinely exploitable.

Run reach.mark_all() (scripts/run_reachability.py) first; without from_entry and
reaches_sink this returns nothing.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import paths  # noqa: E402
from graph_core.store import GraphStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--kinds", nargs="*", default=None,
                    help="sink kinds to walk to; default = all dangerous kinds")
    ap.add_argument("--max-depth", type=int, default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--show", type=int, default=10)
    a = ap.parse_args()

    store = GraphStore()
    try:
        marked = store.read(
            """
            MATCH (f:Function {repo: $repo})
            WHERE f.from_entry = true AND f.reaches_sink = true
            RETURN count(f) AS n
            """, repo=a.repo)
        n = marked[0]["n"] if marked else 0
        print(f"universe: {n:,} functions")
        if not n:
            print("  Empty — run scripts/run_reachability.py first.")
            return

        # Hubs are excluded from paths: a function called from thousands of
        # places carries no path-specific meaning, and routing through one turns
        # every path into every other path.
        hubs = paths.find_hubs(store, a.repo)
        print(f"hubs excluded: {len(hubs):,}")

        kw = dict(repo=a.repo, sink_kinds=a.kinds, limit=a.limit,
                  hub_ids=[h["id"] for h in hubs])
        if a.max_depth is not None:
            kw["max_depth"] = a.max_depth
        rows = paths.sink_paths(store, **kw)
        print(f"raw paths: {len(rows):,}")

        rows = paths.dedupe_paths(rows)
        print(f"after dedupe: {len(rows):,}")
        if not rows:
            print("\n  No paths. Either the universe has no entry->sink route "
                  "within max_depth,\n  or _TRUSTED is excluding the edges that "
                  "would connect them.")
            return

        by_sink = collections.Counter(
            (r.get("sink_name") or r.get("sink") or "?") for r in rows)
        print("\ntop sinks by path count:")
        for name, c in by_sink.most_common(10):
            print(f"  {c:>5}  {name}")

        print(f"\nfirst {a.show} paths:")
        for r in rows[:a.show]:
            chain = r.get("chain") or r.get("names") or []
            if isinstance(chain, list) and chain:
                rendered = " -> ".join(str(c) for c in chain)
            else:
                rendered = str({k: v for k, v in r.items() if k != "lines"})[:200]
            print(f"  [{r.get('kind', '?')}] {rendered}")

        batches = paths.batch_paths(rows)
        print(f"\n{len(batches):,} Pass B batches (paths sharing a sink stay "
              f"together)")
    finally:
        store.close()


if __name__ == "__main__":
    main()

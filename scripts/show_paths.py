"""Enumerate entry->sink paths from the marked graph. No LLM.

    python scripts/show_paths.py --repo experiment
    python scripts/show_paths.py --repo experiment --min-hops 1
    python scripts/show_paths.py --repo experiment --kinds db_execute --show 15

The step after reachability: reach.mark_all() decides WHICH functions can lie on
a source->sink path; this walks the actual paths inside that subgraph. Still
fully deterministic -- the LLM only enters afterwards, to judge exploitability.

Run scripts/run_reachability.py first; without from_entry/reaches_sink this
returns nothing.

WHY --min-hops EXISTS, AND WHY YOU WILL USUALLY WANT IT
sink_paths orders by hops ascending and stops at `limit`. On a JSP-heavy
codebase there are thousands of ZERO-hop paths -- a `_jspService` that calls
executeQuery in the same body, entry and sink being the same function -- and
they consume the entire budget before a single multi-hop chain is returned.
Those 0-hop hits are real findings (a JSP interpolating a request parameter into
SQL is textbook injection), but they are found by looking at one function, not by
path analysis. `--min-hops 1` asks the question path analysis is actually for:
which chains cross function boundaries.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import paths  # noqa: E402
from graph_core.store import GraphStore  # noqa: E402

# Per-hop strategy -> how far that hop can be trusted, worst-first.
#
# Scoring exists because sink_paths returns up to `limit` paths with no quality
# signal beyond length, so a consumer would have to treat a compiler-proven chain
# and a name-guess chain as equally credible. They are not.
_STRATEGY_TRUST = {
    "bytecode": 1.00,             # what javac resolved; not an inference
    "receiver_type": 0.80,        # declared receiver type matched exactly
    "receiver_type_inherited": 0.75,
    "same_scope": 0.70,
    "same_file": 0.65,
    "receiver_type_hint": 0.55,   # inferred hint, then arity
    "imports_qualified": 0.55,
    "imports": 0.45,
    "name": 0.10,                 # ~5% precision; _TRUSTED excludes it, scored
                                  # only so an unexpected appearance is visible
}


def _trust(strategy: str) -> float:
    if not strategy:
        return 0.30
    base = strategy.split("+")[0]          # drop the "+arity" suffix
    if base in _STRATEGY_TRUST:
        return _STRATEGY_TRUST[base]
    for k, v in sorted(_STRATEGY_TRUST.items(), key=lambda kv: -len(kv[0])):
        if base.startswith(k):
            return v
    return 0.30


def score_path(row: dict) -> tuple[float, str]:
    """(score, why). A chain is only as good as its WEAKEST hop -- the minimum,
    not the average, which would let one fabricated hop hide behind nine solid
    ones."""
    strategies = row.get("strategies") or []
    if not strategies:
        # Zero-hop path: entry and sink are the same function, so there is no
        # edge to judge. The catalog match itself is the evidence, and that is
        # bytecode-grade, so this is high confidence rather than unknown.
        return (0.95, "0-hop (sink called directly in the entry function)")
    trusts = [_trust(s) for s in strategies]
    score = min(trusts)
    weakest = strategies[trusts.index(score)] or "?"
    why = f"weakest hop: {weakest}"
    if row.get("has_sanitizer"):
        score *= 0.5
        why += " + sanitizer on path"
    hops = row.get("hops") or 0
    if hops > 6:
        score *= 0.9
        why += f" + long ({hops} hops)"
    return round(score, 3), why


def _sink_label(r: dict) -> str:
    names = r.get("sink_names") or []
    if names:
        return ",".join(str(n) for n in names[:2])
    return str(r.get("sink_fqn") or "?").rsplit("#", 1)[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--kinds", nargs="*", default=None)
    ap.add_argument("--max-depth", type=int, default=None)
    ap.add_argument("--min-hops", type=int, default=0,
                    help="drop paths shorter than this. Use 1 to skip the "
                         "0-hop JSP hits that otherwise consume the whole limit.")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--show", type=int, default=10)
    a = ap.parse_args()

    store = GraphStore()
    try:
        rows_n = store.read(
            """
            MATCH (f:Function {repo: $repo})
            WHERE f.from_entry = true AND f.reaches_sink = true
            RETURN count(f) AS n
            """, repo=a.repo)
        n = rows_n[0]["n"] if rows_n else 0
        print(f"universe: {n:,} functions")
        if not n:
            print("  Empty -- run scripts/run_reachability.py first.")
            return

        hubs = paths.find_hubs(store, a.repo)
        print(f"hubs excluded: {len(hubs):,}")

        kw = dict(repo=a.repo, sink_kinds=a.kinds, limit=a.limit,
                  hub_ids=[h["id"] for h in hubs])
        if a.max_depth is not None:
            kw["max_depth"] = a.max_depth
        rows = paths.sink_paths(store, **kw)
        print(f"raw paths: {len(rows):,}")

        dist = collections.Counter(r.get("hops") or 0 for r in rows)
        print("hop distribution:", dict(sorted(dist.items())))
        if len(rows) >= a.limit and dist.get(0, 0) > a.limit // 2:
            print(f"\n  WARNING: {dist[0]:,} of {len(rows):,} returned paths are "
                  f"0-hop, and the {a.limit}-path limit was hit.\n"
                  f"  Results are ordered by hops, so multi-hop chains were "
                  f"never reached. Re-run with --min-hops 1.")

        if a.min_hops:
            rows = [r for r in rows if (r.get("hops") or 0) >= a.min_hops]
            print(f"after --min-hops {a.min_hops}: {len(rows):,}")

        rows = paths.dedupe_paths(rows)
        print(f"after dedupe: {len(rows):,}")
        if not rows:
            print("\n  No paths left.")
            return

        scored = sorted(((score_path(r), r) for r in rows), key=lambda t: -t[0][0])
        buckets = collections.Counter(
            "high  (>=0.8)" if sc >= 0.8 else
            "med   (0.5-0.8)" if sc >= 0.5 else
            "low   (<0.5)" for (sc, _w), _r in scored)
        print("\npath confidence (min hop trust, sanitizer/length adjusted):")
        for b in ("high  (>=0.8)", "med   (0.5-0.8)", "low   (<0.5)"):
            if buckets.get(b):
                print(f"  {buckets[b]:>6}  {b}")

        by_sink = collections.Counter(_sink_label(r) for r in rows)
        print("\ntop sinks by path count:")
        for name, c in by_sink.most_common(10):
            print(f"  {c:>6}  {name}")

        print(f"\ntop {a.show} paths by confidence:")
        for (sc, why), r in scored[:a.show]:
            fqns = r.get("fqns") or []
            short = [str(f).rsplit(".", 1)[-1] for f in fqns]
            chain = " -> ".join(short) if short else "?"
            kinds = ",".join(r.get("sink_kinds") or []) or "?"
            print(f"  [{sc:.2f}] [{kinds}] ({r.get('hops', 0)}h) {chain[:150]}")
            print(f"          sink={_sink_label(r)}  {why}")

        batches = paths.batch_paths(rows)
        print(f"\n{len(batches):,} Pass B batches (paths sharing a sink stay together)")
    finally:
        store.close()


if __name__ == "__main__":
    main()

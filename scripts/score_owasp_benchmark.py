"""Score the graph's taint marking against OWASP Benchmark's labelled results.

    python scripts/score_owasp_benchmark.py \
        --expected /path/to/BenchmarkJava/expectedresults-1.2.csv \
        --repo benchmark

WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT
Only the CATALOG: for each labelled test case, did ingest mark the right function
with the right vulnerability category? No LLM, no path enumeration, no dataflow —
so the number is a property of the catalog alone and moves only when the catalog
does.

That isolation is the point. A full pipeline score mixes catalog coverage, path
enumeration and a non-deterministic model into one number, and when it drops you
cannot tell which of the three regressed. Measure the deterministic layer first.

WHAT A RESULT MEANS
  recall     of the real vulnerabilities, how many had their sink recognised.
             Low recall = missing catalog entries, and the per-category table
             says exactly which ones.
  precision  of the cases flagged, how many were real. Benchmark pairs almost
             every true case with a near-identical false one that differs only by
             a sanitizer, so precision here is largely a SANITIZER score.

An important honest limit: this measures "was the sink seen", not "does tainted
data reach it". Precision will therefore look WORSE than a real taint analysis
would, because the false cases usually contain the same sink call, just
neutralised. Read precision as a lower bound and recall as the real signal until
dataflow exists.

Benchmark is also synthetic and Spring-flavoured, whereas the target here is a
legacy Ant/servlet app. A good score is a floor, not a promise.
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_core.store import GraphStore  # noqa: E402

# Benchmark category -> the catalog categories that should fire for it. Several
# map to more than one: a "hash" case is weak crypto, and a "trustbound" case can
# surface as either the trust-boundary entry or a response sink.
CATEGORY_MAP = {
    "sqli": {"CWE-89/sql-injection"},
    "cmdi": {"CWE-78/command-injection", "CWE-94/code-injection"},
    "xss": {"CWE-79/xss"},
    "pathtraver": {"CWE-22/path-traversal"},
    "ldapi": {"CWE-90/ldap-injection"},
    "xpathi": {"CWE-643/xpath-injection"},
    "crypto": {"CWE-327/weak-crypto"},
    "hash": {"CWE-327/weak-crypto"},
    "weakrand": {"CWE-330/weak-random"},
    "trustbound": {"CWE-501/trust-boundary", "CWE-113/response-splitting"},
    "securecookie": {"CWE-614/insecure-cookie", "CWE-113/response-splitting"},
}


def load_expected(path: str) -> dict[str, tuple[str, bool]]:
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            # The header line starts with '#'; blank rows appear at the tail.
            if not row or row[0].startswith("#") or len(row) < 3:
                continue
            out[row[0].strip()] = (row[1].strip(), row[2].strip().lower() == "true")
    return out


def load_marks(store: GraphStore, repo: str) -> dict[str, set[str]]:
    """{test-case name: catalog categories marked anywhere in that file}.

    Keyed on the FILE, not the function: a Benchmark case is one file, and the
    sink often sits in a helper the test calls rather than in the entry method.
    """
    rows = store._run(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.taint_categories IS NOT NULL AND size(f.taint_categories) > 0
        RETURN f.file AS file, f.taint_categories AS cats
        """,
        repo=repo,
    )
    by_case: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        base = os.path.basename(r["file"] or "")
        if not base.endswith(".java"):
            continue
        by_case[base[:-5]].update(r["cats"] or [])
    return by_case


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expected", required=True)
    ap.add_argument("--repo", default="benchmark")
    a = ap.parse_args()

    expected = load_expected(a.expected)
    store = GraphStore()
    try:
        marks = load_marks(store, a.repo)
    finally:
        store.close()

    if not marks:
        print("NO taint marks found in the graph for repo "
              f"{a.repo!r}.\nEither the ingest ran with GRAPH_CATALOG_EXTERNAL=off, "
              "or the build predates the marking work. Nothing to score.")
        return

    per_cat: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    unseen_files = 0
    for case, (category, is_real) in expected.items():
        wanted = CATEGORY_MAP.get(category)
        if wanted is None:
            per_cat[category]["unmapped"] += 1
            continue
        got = marks.get(case)
        if got is None:
            unseen_files += 1
        flagged = bool(got and (got & wanted))
        c = per_cat[category]
        c["total"] += 1
        if is_real and flagged:
            c["tp"] += 1
        elif is_real and not flagged:
            c["fn"] += 1
        elif not is_real and flagged:
            c["fp"] += 1
        else:
            c["tn"] += 1

    print(f"{'category':<14}{'cases':>7}{'TP':>7}{'FP':>7}{'FN':>7}"
          f"{'recall':>9}{'prec':>8}")
    print("-" * 60)
    tot = collections.Counter()
    for cat in sorted(per_cat):
        c = per_cat[cat]
        if not c["total"]:
            print(f"{cat:<14}{'(unmapped: no catalog category)':>45}")
            continue
        tot.update(c)
        rec = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else 0.0
        pre = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0.0
        print(f"{cat:<14}{c['total']:>7}{c['tp']:>7}{c['fp']:>7}{c['fn']:>7}"
              f"{rec:>8.1%}{pre:>8.1%}")
    print("-" * 60)
    rec = tot["tp"] / (tot["tp"] + tot["fn"]) if (tot["tp"] + tot["fn"]) else 0.0
    pre = tot["tp"] / (tot["tp"] + tot["fp"]) if (tot["tp"] + tot["fp"]) else 0.0
    print(f"{'TOTAL':<14}{tot['total']:>7}{tot['tp']:>7}{tot['fp']:>7}"
          f"{tot['fn']:>7}{rec:>8.1%}{pre:>8.1%}")

    if unseen_files:
        # Distinguishes "the catalog missed it" from "ingest never saw the file",
        # which look identical in the numbers above and have completely different
        # fixes.
        print(f"\n{unseen_files} labelled case(s) had NO marked function in the "
              f"graph at all.\nIf that number is large the problem is ingest "
              f"coverage, not the catalog — check the build actually indexed "
              f"BenchmarkJava's testcode directory.")
    print("\nRecall is the catalog signal. Precision is a LOWER BOUND — this "
          "scores\n'was the sink recognised', and Benchmark's false cases "
          "usually contain the\nsame sink call with a sanitizer applied, which "
          "only dataflow can distinguish.")


if __name__ == "__main__":
    main()

"""Diff javac's ground-truth call bindings against the graph's CALLS edges.

Gives the number this project has never had: how accurate the Java call graph
actually is, split by confidence — so "88% ambiguous" stops being a proxy for
quality and becomes a measured precision figure.

    javac -d out scripts/oracle/CallOracle.java
    java -cp out CallOracle <source-root> > calls.tsv
    python scripts/oracle/compare_to_graph.py calls.tsv --repo <namespace>

Reads Neo4j via the same env vars the pipeline uses (NEO4J_URI/USER/PASSWORD).

MATCHING — read before trusting the numbers
Pairs are compared as (caller_fqn, callee_fqn) where fqn is `package.Class#method`.
The Java extractor gives every OVERLOAD the same fqn (only the node id carries
the parameter list), so overloads collapse into one pair on both sides. A graph
edge to the wrong overload of the right method therefore counts as correct.
This measures "did it find the right method by name on the right class", which
is the failure mode the heuristic actually has — it does not measure overload
selection.

UNIVERSE
Only caller classes javac actually attributed are compared. A file javac could
not resolve (cyclic missing externals) would otherwise show up as pure recall
loss for the graph, which would be a lie about the graph rather than a fact.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict


def _norm_method(cls_fqn: str, method: str) -> str:
    """javac calls constructors `<init>`; the Java extractor names them after
    their class (java.py, `is_ctor` branch). Without this, EVERY constructor
    call counted as a graph miss — a measurement artifact, not a real defect."""
    if method == "<init>":
        return cls_fqn.rsplit(".", 1)[-1]
    return method


def load_oracle(path: str):
    """(caller_fqn, callee_fqn) pairs javac resolved, plus the caller universe."""
    pairs: set[tuple[str, str]] = set()
    caller_classes: set[str] = set()
    rows = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            # 8 columns since caller arity was added; tolerate the older
            # 7-column files rather than silently reading zero rows.
            if len(parts) >= 8:
                caller_cls, caller_m, _car, callee_cls, callee_m, _ar, file, ln = parts[:8]
            elif len(parts) == 7:
                caller_cls, caller_m, callee_cls, callee_m, _ar, file, ln = parts
            else:
                continue
            if not caller_cls or not callee_cls:
                continue
            rows += 1
            caller_classes.add(caller_cls)
            pairs.add((
                f"{caller_cls}#{_norm_method(caller_cls, caller_m)}",
                f"{callee_cls}#{_norm_method(callee_cls, callee_m)}",
            ))
    return pairs, caller_classes, rows


def load_graph(repo: str):
    """CALLS edges as (caller_fqn, callee_fqn) -> set of confidences."""
    try:
        from neo4j import GraphDatabase
    except ImportError:
        sys.exit("neo4j driver not installed: pip install neo4j")

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD", "neo4j")

    edges: dict[tuple[str, str], set[str]] = defaultdict(set)
    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    with driver.session() as s:
        result = s.run(
            "MATCH (a:CodeNode {repo:$repo, label:'Function'})"
            "-[r:CALLS]->(b:CodeNode {label:'Function'}) "
            "RETURN a.fqn AS caller, b.fqn AS callee, r.confidence AS conf",
            repo=repo,
        )
        for rec in result:
            if rec["caller"] and rec["callee"]:
                edges[(rec["caller"], rec["callee"])].add(rec["conf"] or "")
    driver.close()
    return edges


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("oracle_tsv")
    ap.add_argument("--repo", required=True, help="graph namespace (repo tag)")
    args = ap.parse_args()

    truth, caller_universe, rows = load_oracle(args.oracle_tsv)
    print(f"oracle: {rows:,} resolved invocations -> {len(truth):,} distinct "
          f"(caller,callee) pairs across {len(caller_universe):,} caller classes")

    graph = load_graph(args.repo)
    print(f"graph : {len(graph):,} distinct (caller,callee) CALLS pairs")

    # Restrict both sides to callers javac attributed (see UNIVERSE above).
    def in_universe(pair):
        cls = pair[0].rsplit("#", 1)[0]
        return cls in caller_universe

    truth_u = {p for p in truth if in_universe(p)}
    graph_u = {p for p in graph if in_universe(p)}
    print(f"universe: {len(truth_u):,} truth pairs, {len(graph_u):,} graph pairs\n")

    def report(label: str, got: set):
        tp = len(got & truth_u)
        fp = len(got - truth_u)
        fn = len(truth_u - got)
        prec = 100.0 * tp / (tp + fp) if (tp + fp) else 0.0
        rec = 100.0 * tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
        print(f"{label:<28} pairs={len(got):>9,}  TP={tp:>8,}  FP={fp:>9,}  "
              f"FN={fn:>8,}  precision={prec:5.1f}%  recall={rec:5.1f}%  F1={f1:5.1f}%")

    report("ALL CALLS", graph_u)
    # The split that matters: are the edges we CLAIM are confident actually right,
    # and how much of the ambiguous pile is real?
    confident = {p for p in graph_u if graph[p] - {"AMBIGUOUS"}}
    ambiguous_only = {p for p in graph_u if graph[p] == {"AMBIGUOUS"}}
    report("  confident (non-AMBIG)", confident)
    report("  AMBIGUOUS only", ambiguous_only)

    missed = truth_u - graph_u
    if missed:
        print(f"\nsample of {min(15, len(missed))} real calls the graph MISSED:")
        for caller, callee in sorted(missed)[:15]:
            print(f"  {caller}  ->  {callee}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

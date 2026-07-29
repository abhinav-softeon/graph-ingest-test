"""Guardrail tests for the SlimEdge derive-read contract (TIER3_MEMORY_PLAN.md §11).

A+B Step 2 projects the pre-derive edge set to SlimEdge once it's durable in
Neo4j (Step 1). If a derive pass in pipeline.py ever starts reading a *bulky*
Edge field (confidence, origin, extractor, strategy, ...)
that SlimEdge drops, the slim path would silently produce a worse graph — this
is exactly how the evidence_file/evidence_line dependency in
_synthesize_polymorphic_calls was found: a naive (type, src, dst)-only
projection would have dropped it silently, and it isn't part of the golden
edge-set hash (§7) so the fingerprint oracle wouldn't have caught it either.

Runnable standalone (`python test_slim_edge.py`) or under pytest — it only
needs the stdlib `ast` module and models.py (which has no third-party
imports)."""
from __future__ import annotations

import ast
import dataclasses
import os

from ..models import (
    SLIM_EDGE_FIELDS,
    Edge,
    SlimEdge,
)

_GRAPH_CORE = os.path.join(os.path.dirname(__file__), "..")

# Files×functions that read edges which may be SlimEdge under the streaming
# writer (Step 11: structural edges are slimmed before resolve; resolved edges
# are slimmed by the sink; everything post-resolve sees a fully slim list).
# pipeline._build_package_tree/_derive_sql_links don't take an edges arg —
# excluded. None means "whole file" (resolver reads edges at its top-level pass
# function).
# "dataflow.py" was listed here until the DFG pass was removed entirely (item
# #10); "_synthesize_polymorphic_calls" until polymorphic dispatch moved into
# the database (item #12). _derive_overrides is the last in-Python edge reader.
_SLIM_READERS: dict[str, set[str] | None] = {
    "pipeline.py": {"_derive_overrides"},
    "resolver.py": None,
}

# Variable names bound to an Edge (or Edge candidate) inside those functions.
_EDGE_VARS = {"e", "ce", "support"}


def _edge_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(Edge)}


def test_slimedge_fields_match_contract():
    """SlimEdge's fields are exactly SLIM_EDGE_FIELDS (one source of truth)."""
    slim_fields = {f.name for f in dataclasses.fields(SlimEdge)}
    assert slim_fields == set(SLIM_EDGE_FIELDS), (
        f"SlimEdge fields {slim_fields} != SLIM_EDGE_FIELDS {set(SLIM_EDGE_FIELDS)}"
    )


def test_slim_fields_are_real_edge_fields():
    """Every slim field actually exists on Edge (so to_slim can copy it)."""
    missing = set(SLIM_EDGE_FIELDS) - _edge_fields()
    assert not missing, f"SLIM_EDGE_FIELDS not present on Edge: {missing}"


def test_to_slim_copies_contract_fields():
    e = Edge(
        "CALLS", "src1", "dst1",
        confidence="AMBIGUOUS", origin="DERIVED", extractor="heuristic",
        evidence_file="pkg/foo.py", evidence_line=42, evidence_col=7,
        strategy="polymorphic_dispatch",
    )
    slim = e.to_slim()
    for f in SLIM_EDGE_FIELDS:
        assert getattr(slim, f) == getattr(e, f), f"to_slim mismatch on {f}"
    assert not hasattr(slim, "origin")  # bulky fields genuinely absent
    assert not hasattr(slim, "strategy")


def _forbidden_reads() -> list[str]:
    """Return 'file:func:line:var.attr' for every attribute read, inside a
    slim-reader scope, on an edge-bound variable that targets an Edge field
    OUTSIDE the slim contract."""
    forbidden = _edge_fields() - set(SLIM_EDGE_FIELDS)
    hits: list[str] = []
    for fname, funcs in _SLIM_READERS.items():
        path = os.path.abspath(os.path.join(_GRAPH_CORE, fname))
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fname)
        if funcs is None:
            scopes = [("<module>", tree)]
        else:
            scopes = [
                (fn.name, fn) for fn in ast.walk(tree)
                if isinstance(fn, ast.FunctionDef) and fn.name in funcs
            ]
        for scope_name, scope in scopes:
            for node in ast.walk(scope):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in _EDGE_VARS
                    and node.attr in forbidden
                ):
                    hits.append(f"{fname}:{scope_name}:{node.lineno}: {node.value.id}.{node.attr}")
    return hits


def test_slim_readers_never_read_bulky_edge_fields():
    """No slim-reading scope may read an Edge field the slim projection
    drops."""
    hits = _forbidden_reads()
    assert not hits, (
        "Code that can receive SlimEdge objects under the streaming writer "
        "reads an Edge field outside the SlimEdge contract — the slim "
        "projection (A+B Steps 2/11) would silently degrade the graph. Add "
        "the field to SLIM_EDGE_FIELDS (and Edge.to_slim) if it's genuinely "
        "needed:\n  " + "\n  ".join(hits)
    )


if __name__ == "__main__":
    test_slimedge_fields_match_contract()
    test_slim_fields_are_real_edge_fields()
    test_to_slim_copies_contract_fields()
    test_slim_readers_never_read_bulky_edge_fields()
    print("all guardrail tests passed ✓")

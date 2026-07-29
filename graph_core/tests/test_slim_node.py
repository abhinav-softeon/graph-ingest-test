"""Guardrail tests for the SlimNode contract (TIER3_MEMORY_PLAN.md §4).

`all_nodes` IS a SlimNode list from the pre-resolve write onward (item #14), so
this contract now covers far more than resolution: EVERY consumer from that
point on — resolver.py, the derive passes, validate_graph, scip_resolver — sees
only SLIM_NODE_FIELDS. If any of them starts reading a Node field the projection
drops, it would raise AttributeError at best and silently degrade output at
worst. These tests fail loudly instead, so the guarantee is enforced
mechanically rather than by memory of a manual audit.

Runnable standalone (`python test_slim_node.py`) or under pytest — it only needs
the stdlib `ast` module and models.py (which has no third-party imports)."""
from __future__ import annotations

import ast
import dataclasses
import os

from ..models import (
    SLIM_NODE_FIELDS,
    Node,
    SlimNode,
)

_GRAPH_CORE = os.path.join(os.path.dirname(__file__), "..")
_RESOLVER = os.path.join(_GRAPH_CORE, "resolver.py")

# Everything that reads nodes AFTER the pre-resolve write, where all_nodes has
# already been projected. None means "whole file". `index_repo` is excluded on
# purpose: it reads full Node fields legitimately, BEFORE the projection (the
# extraction/merge phase) and on late_nodes, which stay full.
_POST_RESOLVE_READERS: dict[str, set[str] | None] = {
    "pipeline.py": {
        "_derive_overrides",
        "_build_package_tree",
        "_derive_sql_links",
        "_lowram_derive_and_write",
    },
    "validator.py": None,
    "scip_resolver.py": None,
}

# Variable names bound to a Node (or Node candidate) inside resolver.py. Attribute
# reads on these must stay within the slim contract. Non-node objects (ref, e, cov,
# self, …) are intentionally excluded so we don't flag e.g. `ref.recv`.
_NODE_VARS = {
    "n", "c", "cn", "src_node", "m", "ep", "ccls",
    "cand", "candidate", "node", "dst", "s", "d",
}


def _node_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(Node)}


def test_slimnode_fields_match_contract():
    """SlimNode's fields are exactly SLIM_NODE_FIELDS (one source of truth)."""
    slim_fields = {f.name for f in dataclasses.fields(SlimNode)}
    assert slim_fields == set(SLIM_NODE_FIELDS), (
        f"SlimNode fields {slim_fields} != SLIM_NODE_FIELDS {set(SLIM_NODE_FIELDS)}"
    )


def test_slim_fields_are_real_node_fields():
    """Every slim field actually exists on Node (so to_slim can copy it)."""
    missing = set(SLIM_NODE_FIELDS) - _node_fields()
    assert not missing, f"SLIM_NODE_FIELDS not present on Node: {missing}"


def test_to_slim_copies_contract_fields():
    n = Node(
        id="x", label="Function", name="foo", fqn="pkg.Foo.foo", repo="r",
        kind="method", lang="python", file="pkg/foo.py", package="pkg",
        scope="", param_count=2, method="", route="",
        docstring="BULKY" * 1000, signature="def foo(a, b)",
    )
    slim = n.to_slim()
    for f in SLIM_NODE_FIELDS:
        assert getattr(slim, f) == getattr(n, f), f"to_slim mismatch on {f}"
    assert not hasattr(slim, "docstring")  # bulky field genuinely absent


def _forbidden_reads(path: str, fn_names: set[str] | None) -> list[str]:
    """'file:line: var.attr' for every Load of a Node field outside the slim
    contract, inside the named functions (or the whole file when None)."""
    fname = os.path.basename(path)
    with open(os.path.abspath(path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=fname)
    forbidden = _node_fields() - set(SLIM_NODE_FIELDS)
    hits: list[str] = []

    def scan(scope):
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in forbidden
            ):
                base = node.value
                base_name = base.id if isinstance(base, ast.Name) else ast.unparse(base)
                hits.append(f"{fname}:{node.lineno}: {base_name}.{node.attr}")

    if fn_names is None:
        scan(tree)
    else:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in fn_names:
                scan(node)
    return hits


def test_post_resolve_consumers_never_read_dropped_node_fields():
    """Every pass that runs after the pre-resolve projection must stay inside
    SLIM_NODE_FIELDS — all_nodes holds SlimNode records by then, so anything else
    is an AttributeError waiting for a corpus that exercises it."""
    hits: list[str] = []
    for fname, fns in _POST_RESOLVE_READERS.items():
        hits += _forbidden_reads(os.path.join(_GRAPH_CORE, fname), fns)
    assert not hits, (
        "post-resolve code reads Node fields the slim projection drops. Either "
        "add the field to SLIM_NODE_FIELDS (and to_slim), or read it before the "
        "projection:\n  " + "\n  ".join(hits)
    )


def _forbidden_reads_in_resolver() -> list[str]:
    """Return 'line:var.attr' for every attribute read in resolver.py on a
    node-bound variable that targets a Node field OUTSIDE the slim contract."""
    with open(os.path.abspath(_RESOLVER), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="resolver.py")
    forbidden = _node_fields() - set(SLIM_NODE_FIELDS)
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id in _NODE_VARS
            and node.attr in forbidden
        ):
            hits.append(f"{node.lineno}: {node.value.id}.{node.attr}")
    return hits


def test_resolver_never_reads_bulky_node_fields():
    """resolver.py must not read any Node field the slim projection drops."""
    hits = _forbidden_reads_in_resolver()
    assert not hits, (
        "resolver.py reads Node fields outside the SlimNode contract — the slim "
        "path would silently degrade resolution. Add the field to SLIM_NODE_FIELDS "
        "(and to_slim) if it's genuinely needed:\n  " + "\n  ".join(hits)
    )


if __name__ == "__main__":
    test_slimnode_fields_match_contract()
    test_slim_fields_are_real_node_fields()
    test_to_slim_copies_contract_fields()
    test_resolver_never_reads_bulky_node_fields()
    test_post_resolve_consumers_never_read_dropped_node_fields()
    print("all guardrail tests passed ✓")

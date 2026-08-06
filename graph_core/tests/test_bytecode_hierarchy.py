"""EXTENDS / IMPLEMENTS / OVERRIDES read out of the class hierarchy.

The point of this pass is precision, so most of these tests are negative: they
assert edges that a name+arity match WOULD produce and bytecode must NOT. The
headline case is `handle(String)` against an inherited `handle(int)` — same name,
same arity, different types, so pipeline._derive_overrides cannot tell them apart
(it only has param_count, because param_types is dropped by the slim projection
long before derive runs). A false OVERRIDES does not stay contained: polymorphic
dispatch fans out over it, and consumers join through it at query time.

The fixture keeps everything in one file so a single javac invocation produces
the whole hierarchy; only `Hier` is public, the rest are package-private, which
is what lets three top-level types share Hier.java.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode_resolver import resolve_java_bytecode
from graph_core.discovery import discover
from graph_core.pipeline import _derive_overrides, _extract_one

_SRC = """
package com.acme;

interface Greeter {
    String greet(String who);
    void reset();
}

abstract class Base implements Greeter {
    public String greet(String who) { return "hi " + who; }
    public void handle(int i) { }
    public static void statik() { }
    private void hidden() { }
    public Object copy() { return this; }
    public void reset() { }
}

public class Hier extends Base {
    public String greet(String who) { return "hello " + who; }
    public void handle(String s) { }
    public static void statik() { }
    private void hidden() { }
    public Hier copy() { return this; }
}
"""

_HAVE_JAVAC = shutil.which("javac") is not None
pytestmark = pytest.mark.skipif(not _HAVE_JAVAC, reason="javac not on PATH")


@pytest.fixture(scope="module")
def built():
    """(nodes, edges, synthesized, report) for the hierarchy fixture.

    ``edges`` is extraction's structural edges PLUS the bytecode pass's, which is
    what index_repo actually feeds derive. The extraction half matters more than
    it looks: _derive_overrides builds its class->method parent map from CONTAINS,
    so a fixture that passed bytecode edges alone would make it return nothing at
    all — and every assertion about suppressing it would pass vacuously.
    """
    root = tempfile.mkdtemp(prefix="bchier_")
    pkg = os.path.join(root, "com", "acme")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "Hier.java"), "w", encoding="utf-8") as fh:
        fh.write(_SRC)
    proc = subprocess.run(["javac", "-g", os.path.join("com", "acme", "Hier.java")],
                          cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"javac failed: {proc.stderr[:300]}")

    nodes, extracted = [], []
    for fi in discover(root):
        n, e, _refs = _extract_one(fi, "bench")
        nodes.extend(n)
        extracted.extend(e)
    bc_edges, synth, rep = resolve_java_bytecode(nodes, root, "bench", java_files_seen=1)
    return nodes, extracted + bc_edges, synth, rep


def _index(nodes, synth):
    out = {n.id: n for n in nodes}
    out.update({n.id: n for n in synth})
    return out


def _pairs(edges, etype, index):
    """(src fqn, dst fqn) for every edge of this type whose ends are both known."""
    return [(index[e.src].fqn, index[e.dst].fqn) for e in edges
            if e.type == etype and e.src in index and e.dst in index]


def _method(nodes, fqn, arity):
    hits = [n for n in nodes if n.fqn == fqn and (n.param_count or 0) == arity]
    assert hits, f"no node for {fqn}/{arity}"
    return hits[0]


class TestHierarchyEdges:
    def test_pass_is_kept(self, built):
        _n, _e, _s, rep = built
        assert rep.available, rep.reason

    def test_extends_emitted_for_in_repo_superclass(self, built):
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier", "com.acme.Base") in _pairs(edges, "EXTENDS", _index(nodes, synth))

    def test_implements_emitted_for_in_repo_interface(self, built):
        nodes, edges, synth, _rep = built
        assert ("com.acme.Base", "com.acme.Greeter") in _pairs(
            edges, "IMPLEMENTS", _index(nodes, synth))

    def test_hierarchy_edges_are_extracted_not_inferred(self, built):
        """A compiler binding, not a guess — consumers filter on this."""
        _n, edges, _s, _rep = built
        hier = [e for e in edges if e.type in ("EXTENDS", "IMPLEMENTS")]
        assert hier
        assert all(e.strategy == "bytecode" for e in hier)
        assert all(e.confidence == "EXTRACTED" for e in hier)

    def test_external_supertypes_emit_nothing(self, built):
        """java.lang.Object has no node; the chain ends rather than dangling."""
        nodes, edges, synth, _rep = built
        index = _index(nodes, synth)
        for e in edges:
            if e.type in ("EXTENDS", "IMPLEMENTS"):
                assert e.dst in index, "hierarchy edge pointing outside the graph"

    def test_extends_never_attributed_to_an_outer_class(self, built):
        """Anonymous classes have no node of their own, so owner_class_id falls
        back to the enclosing class. The hierarchy must use the class's OWN node
        or an outer class inherits whatever its callbacks implement."""
        nodes, edges, synth, _rep = built
        for src, dst in _pairs(edges, "EXTENDS", _index(nodes, synth)):
            assert src != dst


class TestOverridesPrecision:
    def test_real_override_is_found(self, built):
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#greet", "com.acme.Base#greet") in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_transitive_interface_override_is_found(self, built):
        """Hier -> Base -> Greeter: the walk must not stop at the direct parent."""
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#greet", "com.acme.Greeter#greet") in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_same_arity_different_types_is_NOT_an_override(self, built):
        """THE case this pass exists for. Hier#handle(String) and
        Base#handle(int) share name and arity, so name+param_count says override.
        The erased descriptors differ, so it is not one."""
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#handle", "com.acme.Base#handle") not in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_static_methods_hide_rather_than_override(self, built):
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#statik", "com.acme.Base#statik") not in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_private_methods_never_override(self, built):
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#hidden", "com.acme.Base#hidden") not in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_covariant_return_is_still_an_override(self, built):
        """`Hier copy()` overrides `Object copy()`. Comparing whole descriptors
        would miss this, which is why only the parameter types are compared."""
        nodes, edges, synth, _rep = built
        assert ("com.acme.Hier#copy", "com.acme.Base#copy") in _pairs(
            edges, "OVERRIDES", _index(nodes, synth))

    def test_overrides_are_extracted_confidence(self, built):
        _n, edges, _s, _rep = built
        ovr = [e for e in edges if e.type == "OVERRIDES"]
        assert ovr
        assert all(e.confidence == "EXTRACTED" and e.strategy == "bytecode" for e in ovr)


class TestAuthorityHandoff:
    def test_chain_is_complete_so_methods_are_authoritative(self, built):
        """Every ancestor here was parsed, so the answer is complete and the
        heuristic must be told to stand down for these methods."""
        nodes, _e, _s, rep = built
        assert rep.override_chains_truncated == 0
        hier_greet = _method(nodes, "com.acme.Hier#greet", 1)
        assert hier_greet.id in rep.authoritative_override_methods

    def test_negative_answers_are_authoritative_too(self, built):
        """handle(String) overrides nothing. That is a RESULT, not a gap — so it
        must be in the authoritative set, or the heuristic re-adds the false
        pair this pass just ruled out."""
        nodes, _e, _s, rep = built
        handle_str = _method(nodes, "com.acme.Hier#handle", 1)
        assert handle_str.id in rep.authoritative_override_methods

    def test_derive_overrides_respects_the_handoff(self, built):
        """End to end: the heuristic pass, given the bytecode handoff, must not
        re-emit the false handle(String)->handle(int) pair."""
        nodes, edges, synth, rep = built
        index = _index(nodes, synth)
        derived = _derive_overrides(
            nodes, edges,
            skip_methods=rep.authoritative_override_methods,
            skip_pairs=rep.emitted_override_pairs,
        )
        pairs = [(index[e.src].fqn, index[e.dst].fqn) for e in derived
                 if e.src in index and e.dst in index]
        assert ("com.acme.Hier#handle", "com.acme.Base#handle") not in pairs

    def test_the_heuristic_alone_WOULD_emit_the_false_pair(self, built):
        """Proves the previous test is testing something. Without the handoff the
        name+param_count match produces exactly the wrong edge — this is the
        regression that shipped before this pass existed."""
        nodes, edges, synth, _rep = built
        index = _index(nodes, synth)
        derived = _derive_overrides(nodes, edges)
        pairs = [(index[e.src].fqn, index[e.dst].fqn) for e in derived
                 if e.src in index and e.dst in index]
        assert ("com.acme.Hier#handle", "com.acme.Base#handle") in pairs

    def test_no_duplicate_pair_across_both_passes(self, built):
        """A pair bytecode already emitted must not come back at INFERRED
        confidence, or MERGE's last-write-wins downgrades it."""
        nodes, edges, synth, rep = built
        derived = _derive_overrides(
            nodes, edges,
            skip_methods=rep.authoritative_override_methods,
            skip_pairs=rep.emitted_override_pairs,
        )
        redundant = {(e.src, e.dst) for e in derived} & rep.emitted_override_pairs
        assert not redundant

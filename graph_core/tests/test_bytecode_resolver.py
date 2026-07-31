"""End-to-end guardrails for the bytecode resolver.

Compiles a fixture, extracts it with tree-sitter, and asserts the two sides join
correctly — which is the whole risk of this design. Bytecode resolves edges and
tree-sitter owns nodes, so a matching bug does not crash: it silently produces
fewer edges, or worse, edges pointing at the wrong overload.

The stale-build guard gets its own test because it protects against the one
failure mode that is invisible by construction: class files from an old build
parse perfectly and yield precise, confident, WRONG edges for code that no
longer exists.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode_resolver import discover_class_sources, resolve_java_bytecode
from graph_core.discovery import discover
from graph_core.pipeline import _extract_one

_SRC = """
package com.acme;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Function;

public class Svc {
    private String name;
    private static int counter;

    static { counter = 1; }

    public Svc(String n) { this.name = n; }

    public String getName() { return name; }          // BARE read, no `this.`
    public void bump() { counter = counter + 1; }

    public void handle(String s) { bump(); }
    public void handle(int i) { counter = i; }
    public void handle(String s, int i) { handle(s); handle(i); }

    public Runnable makeLambda() { return () -> bump(); }

    public Function<String,String> anon() {
        return new Function<String,String>() {
            public String apply(String in) { return getName(); }
        };
    }

    public List<String> build() {
        List<String> out = new ArrayList<>();
        out.add(getName());
        return out;
    }
}
"""

_HAVE_JAVAC = shutil.which("javac") is not None
pytestmark = pytest.mark.skipif(not _HAVE_JAVAC, reason="javac not on PATH")


@pytest.fixture(scope="module")
def built():
    """(nodes, edges, synthesized, report) from compiling + extracting one class."""
    root = tempfile.mkdtemp(prefix="bcres_")
    pkg = os.path.join(root, "com", "acme")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "Svc.java"), "w", encoding="utf-8") as fh:
        fh.write(_SRC)
    proc = subprocess.run(["javac", "-g", os.path.join("com", "acme", "Svc.java")],
                          cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"javac failed: {proc.stderr[:300]}")

    nodes = []
    for fi in discover(root):
        nodes.extend(_extract_one(fi, "bench")[0])
    edges, synth, rep = resolve_java_bytecode(nodes, root, "bench", java_files_seen=1)
    return nodes, edges, synth, rep


def _by_id(nodes, synth):
    out = {n.id: n for n in nodes}
    out.update({n.id: n for n in synth})
    return out


def _edges_of(edges, etype, index):
    return [(index[e.src].fqn, index[e.dst].fqn) for e in edges
            if e.type == etype and e.src in index and e.dst in index]


class TestResolverBasics:
    def test_available_and_clean_match(self, built):
        _nodes, _edges, _synth, rep = built
        assert rep.available, rep.reason
        assert rep.match_rate == 1.0
        assert rep.file_coverage == 1.0
        assert rep.match_stats.get("unmatched_method", 0) == 0
        assert rep.match_stats.get("ambiguous_method", 0) == 0

    def test_no_class_sources_is_graceful(self):
        """Absent bytecode must degrade, never raise — the caller keeps its
        heuristic edges."""
        empty = tempfile.mkdtemp(prefix="bcres_empty_")
        edges, synth, rep = resolve_java_bytecode([], empty, "bench")
        assert (edges, synth) == ([], [])
        assert not rep.available
        assert "no .class" in rep.reason

    def test_discovery_finds_class_dir(self, built):
        _nodes, _edges, _synth, _rep = built
        # discover_class_sources collapses to package roots rather than listing
        # every .class file; a build tree holds tens of thousands.
        assert _rep.class_sources >= 1


class TestCallEdges:
    def test_in_repo_calls_emitted(self, built):
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        calls = _edges_of(edges, "CALLS", index)
        assert ("com.acme.Svc#build", "com.acme.Svc#getName") in calls
        assert ("com.acme.Svc#handle", "com.acme.Svc#bump") in calls

    def test_overloads_resolve_to_distinct_nodes(self, built):
        """handle(String) and handle(int) share fqn AND arity. Only the
        descriptor separates them, and the heuristic resolver cannot."""
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        caller = [n for n in nodes
                  if n.fqn == "com.acme.Svc#handle" and n.param_count == 2][0]
        targets = {e.dst for e in edges if e.type == "CALLS" and e.src == caller.id}
        assert len(targets) == 2, "both overloads must be distinct edges"
        assert all(index[t].fqn == "com.acme.Svc#handle" for t in targets)
        assert {index[t].param_types[0] for t in targets} == {"String", "int"}

    def test_external_calls_are_counted_not_emitted(self, built):
        """java.util.List#add has no node — emitting it would be a false edge.
        Counted instead, which is the raw material for Phase 4 CALLS_EXTERNAL."""
        nodes, edges, synth, rep = built
        index = _by_id(nodes, synth)
        assert rep.external_calls > 0
        for e in edges:
            assert e.src in index and e.dst in index

    def test_edges_are_extracted_confidence(self, built):
        """A compiler binding is an observation, not an inference. Consumers
        filter on strategy='bytecode' to demand Tier 0 for multi-hop paths."""
        _nodes, edges, _synth, _rep = built
        assert edges
        for e in edges:
            assert e.confidence == "EXTRACTED"
            assert e.strategy == "bytecode"


class TestFieldEdges:
    def test_bare_field_read(self, built):
        """HANDOFF 4.4: java.py sees only explicit `this.x`, so `return name;`
        produces no READS at all today."""
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        reads = _edges_of(edges, "READS", index)
        assert ("com.acme.Svc#getName", "com.acme.Svc.name") in reads

    def test_static_read_and_write(self, built):
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        assert ("com.acme.Svc#bump", "com.acme.Svc.counter") in _edges_of(edges, "READS", index)
        assert ("com.acme.Svc#bump", "com.acme.Svc.counter") in _edges_of(edges, "WRITES", index)

    def test_constructor_write(self, built):
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        assert ("com.acme.Svc#Svc", "com.acme.Svc.name") in _edges_of(edges, "WRITES", index)


class TestNodeSynthesis:
    """HANDOFF 4.2 — constructs with no source declaration."""

    def test_lambda_anonymous_and_clinit_synthesized(self, built):
        _nodes, _edges, synth, _rep = built
        kinds = {n.kind for n in synth}
        assert {"lambda", "initializer", "anonymous"} <= kinds

    def test_every_synthesized_node_has_real_positions(self, built):
        """IMPLEMENTATION_PLAN.md invariant 1. A node with fabricated positions
        is worse than a missing one: every consumer treats file+start_line as a
        real place to go read code."""
        _nodes, _edges, synth, _rep = built
        assert synth
        for n in synth:
            assert n.file, f"{n.fqn} has no file"
            assert n.start_line > 0, f"{n.fqn} has no start_line"
            assert n.end_line >= n.start_line, f"{n.fqn} has a bad line range"

    def test_synthesized_nodes_are_contained(self, built):
        """They must hang off a real Class node or they are unreachable."""
        nodes, edges, synth, _rep = built
        synth_ids = {n.id for n in synth}
        contained = {e.dst for e in edges if e.type == "CONTAINS"}
        assert synth_ids <= contained

    def test_lambda_body_calls_are_recovered(self, built):
        """The payoff: this call is invisible to every source-level pass."""
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        lam = [n for n in synth if n.kind == "lambda"][0]
        targets = {index[e.dst].fqn for e in edges
                   if e.type == "CALLS" and e.src == lam.id and e.dst in index}
        assert "com.acme.Svc#bump" in targets

    def test_anonymous_class_calls_are_recovered(self, built):
        nodes, edges, synth, _rep = built
        index = _by_id(nodes, synth)
        anon = [n for n in synth if n.kind == "anonymous" and "apply" in n.name]
        assert anon
        targets = {index[e.dst].fqn for e in edges
                   if e.type == "CALLS" and e.src == anon[0].id and e.dst in index}
        assert "com.acme.Svc#getName" in targets


class TestStaleBuildGuard:
    def test_bytecode_for_unrelated_source_is_discarded(self, built):
        """The failure mode that is invisible by construction: class files from
        an old build parse fine and produce precise edges for dead code.

        Simulated by pairing real, valid bytecode with nodes extracted from a
        DIFFERENT class — exactly what a stale build looks like from here.
        """
        nodes, _edges, _synth, _rep = built
        root = tempfile.mkdtemp(prefix="bcres_other_")
        pkg = os.path.join(root, "com", "other")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "Unrelated.java"), "w", encoding="utf-8") as fh:
            fh.write("package com.other;\npublic class Unrelated { void go() { } }\n")
        proc = subprocess.run(["javac", "-g", os.path.join("com", "other", "Unrelated.java")],
                              cwd=root, capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.skip("javac failed")

        # Unrelated bytecode + Svc's nodes: nothing lines up.
        edges, synth, bad = resolve_java_bytecode(
            nodes, root, "bench", java_files_seen=1,
        )
        assert not bad.available
        assert (edges, synth) == ([], [])
        assert "does not correspond" in bad.reason or "match rate" in bad.reason

    def test_match_rate_floor_rejects(self, built):
        """A floor of 1.01 is unreachable, so a healthy run must still fail it —
        proving the gate is actually consulted rather than decorative."""
        nodes, _edges, _synth, rep = built
        root = tempfile.mkdtemp(prefix="bcres_stale_")
        pkg = os.path.join(root, "com", "acme")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "Svc.java"), "w", encoding="utf-8") as fh:
            fh.write(_SRC)
        subprocess.run(["javac", "-g", os.path.join("com", "acme", "Svc.java")],
                       cwd=root, capture_output=True, text=True)
        edges, synth, strict = resolve_java_bytecode(
            nodes, root, "bench", java_files_seen=1, min_match_rate=1.01,
        )
        assert not strict.available
        assert "match rate" in strict.reason
        assert (edges, synth) == ([], [])

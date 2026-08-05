"""Guards on the generated corpus: it must keep exercising the gap it exists for.

The corpus is only useful if it keeps a specific property — that a GRAPH-ONLY leak
detector scores high precision and roughly half recall, with the misses being
exactly the release-present-but-skipped-on-exception shape. If a change to
external_api or the resolver ever makes the graph score 100% recall here, either the
graph got a new capability (great, and worth knowing) or the corpus stopped
containing the hard case (bad, and silent). Either way this test should fail and
make someone look.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode_resolver import discover_class_sources, resolve_java_bytecode
from graph_core.discovery import discover
from graph_core.javac_autocompile import compile_tree, javac_available
from graph_core.pipeline import _extract_one
from scripts.gen_test_corpus import generate

_HAS_JAVAC = javac_available()


@pytest.fixture(scope="module")
def corpus():
    """Small deterministic corpus. Sized down from the default for test speed while
    keeping every shape represented — SHAPES cycles with period 4, so 12 DAOs covers
    all four resource shapes three times each."""
    out = tempfile.mkdtemp(prefix="corpus_test_")
    try:
        manifest = generate(out, daos=12, services=6, endpoints=6, handlers=4, pools=4)
        yield out, manifest
    finally:
        shutil.rmtree(out, ignore_errors=True)


class TestGeneration:
    def test_emits_expected_shape(self, corpus):
        _root, m = corpus
        assert m["counts"]["files"] > 30
        # All four resource shapes must be present, or the precision/recall split
        # this corpus exists to demonstrate collapses.
        shapes = {d["shape"] for d in m["expected_dao_findings"]}
        assert shapes == {"CLEAN_FINALLY", "LEAK_NO_FINALLY", "CLEAN_TWR", "LEAK_NO_CLOSE"}

    def test_manifest_records_the_invisible_subset(self, corpus):
        """The whole point: leaks where a release IS present so the graph is
        satisfied. Zero here means the corpus no longer tests the summary layer."""
        _root, m = corpus
        assert m["expected"]["leaks_graph_cannot_see"] > 0
        for d in m["expected_dao_findings"]:
            if d["shape"] == "LEAK_NO_FINALLY":
                assert d["expect_leak"] and d["graph_sees_a_release"]

    def test_sanitized_paths_are_not_vulnerable(self, corpus):
        """Sanitized concatenation must be marked non-vulnerable, otherwise a
        detector that ignores sanitizers scores perfectly and precision is untested."""
        _root, m = corpus
        san = [d for d in m["expected_dao_findings"] if d["sanitized"]]
        assert san, "corpus has no sanitized paths — precision is not being tested"
        assert all(not d["expect_injection"] for d in san)

    def test_deterministic(self):
        """Same inputs, same corpus — otherwise measurements are not comparable
        across runs and regoldening is meaningless."""
        a = tempfile.mkdtemp(prefix="det_a_")
        b = tempfile.mkdtemp(prefix="det_b_")
        try:
            ma = generate(a, daos=6, services=3, endpoints=3, handlers=2, pools=2)
            mb = generate(b, daos=6, services=3, endpoints=3, handlers=2, pools=2)
            assert ma["expected"] == mb["expected"]
            sample = os.path.join("com", "testcorp", "dao", "Dao1.java")
            assert (open(os.path.join(a, sample), encoding="utf-8").read()
                    == open(os.path.join(b, sample), encoding="utf-8").read())
        finally:
            shutil.rmtree(a, ignore_errors=True)
            shutil.rmtree(b, ignore_errors=True)


@pytest.mark.skipif(not _HAS_JAVAC, reason="javac not on PATH")
class TestCompileAndResolve:
    def test_compiles_clean(self, corpus):
        root, _m = corpus
        out, rep = compile_tree(root)
        assert out is not None, rep.get("reason")
        assert rep["classes"] >= rep["sources"]   # inner classes add to the count

    def test_debug_info_is_present(self, corpus):
        """-g is the difference between synthesizing lambda/anonymous bodies and
        silently losing them. Asserted on the artifact, not on the flag."""
        root, _m = corpus
        out, _rep = compile_tree(root)
        target = os.path.join(out, "com", "testcorp", "dao", "Dao1.class")
        got = subprocess.run(["javap", "-l", target], capture_output=True, text=True)
        assert "LineNumberTable" in got.stdout

    def test_bytecode_resolves_at_full_match_rate(self, corpus):
        root, _m = corpus
        nodes, nf = [], 0
        for fi in discover(root):
            nodes += _extract_one(fi, "corpus")[0]
            nf += 1
        class_root, _rep = compile_tree(root)
        edges, synth, rep = resolve_java_bytecode(
            nodes, root, "corpus", class_roots=[class_root], java_files_seen=nf)
        assert rep.available, rep.reason
        # A self-contained tree compiled from its own sources should line up exactly.
        # Anything below this means the matcher is losing methods it should find.
        assert rep.match_rate > 0.95
        assert rep.call_edges > 0
        # Lambdas and anonymous classes must become real nodes, and none may be
        # dropped for want of line numbers.
        kinds = {n.kind for n in synth if n.label == "Function"}
        assert {"lambda", "anonymous"} <= kinds
        assert rep.synthesis_skipped_no_lines == 0

    def test_return_type_rule_finds_pool_wrappers(self, corpus):
        """The non-obvious rule: acquisition is detected by RETURN TYPE, so in-repo
        factories on non-JDBC classes with four different names all count. An
        owner-based rule would find none of them."""
        root, _m = corpus
        nodes = []
        for fi in discover(root):
            nodes += _extract_one(fi, "corpus")[0]
        class_root, _rep = compile_tree(root)
        edges, synth, _r = resolve_java_bytecode(
            nodes, root, "corpus", class_roots=[class_root])
        byid = {n.id: n for n in synth}
        acquires = {byid[e.dst].fqn for e in edges
                    if e.type == "CALLS_EXTERNAL"
                    and e.dst in byid and byid[e.dst].kind == "db_acquire"}
        found_names = {a.rsplit("#", 1)[-1] for a in acquires}
        assert {"getConnection", "getDbConn", "getCon", "getDbConnection"} <= found_names

    def test_graph_alone_is_precise_but_half_blind(self, corpus):
        """THE regression guard. The graph should flag only real leaks (precision 1.0)
        and miss exactly the release-present ones. If recall ever hits 1.0 here,
        either the graph gained a capability or the corpus lost its hard case —
        both need a human to look."""
        root, m = corpus
        nodes = []
        for fi in discover(root):
            nodes += _extract_one(fi, "corpus")[0]
        class_root, _rep = compile_tree(root)
        edges, synth, _r = resolve_java_bytecode(
            nodes, root, "corpus", class_roots=[class_root])
        byid = {n.id: n for n in list(nodes) + list(synth)}

        acq, rel = set(), set()
        for e in edges:
            if e.type != "CALLS_EXTERNAL" or e.dst not in byid:
                continue
            kind = byid[e.dst].kind
            if kind == "db_acquire":
                acq.add(e.src)
            elif kind == "db_release":
                rel.add(e.src)

        flagged = {byid[f].fqn for f in acq - rel}
        expected = {d["fqn"]: d for d in m["expected_dao_findings"]}
        truth = {f for f, d in expected.items() if d["expect_leak"]}
        clean = {f for f, d in expected.items() if not d["expect_leak"]}

        assert not (flagged & clean), (
            f"graph flagged genuinely-clean functions: {sorted(flagged & clean)}")
        assert flagged & truth, "graph found no real leaks at all"
        missed = truth - flagged
        assert len(missed) == m["expected"]["leaks_graph_cannot_see"], (
            f"expected {m['expected']['leaks_graph_cannot_see']} graph-invisible "
            f"leaks, got {len(missed)}"
        )
        assert all(expected[f]["shape"] == "LEAK_NO_FINALLY" for f in missed), (
            "the graph's misses should be exactly the release-present shape")

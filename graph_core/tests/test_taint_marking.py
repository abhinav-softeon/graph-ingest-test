"""End-to-end taint marking, on REAL compiled bytecode.

Everything else about the catalog is table lookups. This compiles Java, reads the
class file, and asserts the marks land on the right Function nodes with the right
lines and argument positions — the only test here that would catch the catalog
being wired up correctly but never actually firing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode_resolver import resolve_java_bytecode
from graph_core.discovery import discover
from graph_core.pipeline import _apply_taint_marks, _extract_one

_HAVE_JAVAC = shutil.which("javac") is not None
pytestmark = pytest.mark.skipif(not _HAVE_JAVAC, reason="javac not on PATH")

# A textbook SQL injection plus a clean, parameterized control in the same class.
# The control matters: a catalog that flags both is useless, and that failure is
# invisible if every fixture is vulnerable.
_SRC = """
package com.acme;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Statement;

public class Dao {
    public String vulnerable(javax.servlet.http.HttpServletRequest req,
                             Connection conn) throws Exception {
        String id = req.getParameter("id");
        String sql = "SELECT * FROM users WHERE id = '" + id + "'";
        Statement st = conn.createStatement();
        st.executeQuery(sql);
        return sql;
    }

    public void safe(Connection conn, String id) throws Exception {
        PreparedStatement ps = conn.prepareStatement("SELECT * FROM users WHERE id = ?");
        ps.setString(1, id);
        ps.executeQuery();
    }

    public int noTaintAtAll(int a, int b) {
        return a + b;
    }
}
"""

# Minimal servlet API stub so javac can resolve the import without a container
# jar. Only the signature matters — bytecode records the declared owner type from
# the descriptor, which is what the catalog is keyed on.
_SERVLET_STUB = """
package javax.servlet.http;
public interface HttpServletRequest {
    String getParameter(String name);
}
"""


@pytest.fixture(scope="module")
def marked():
    root = tempfile.mkdtemp(prefix="taintmark_")
    for pkg, fname, src in (
        (("javax", "servlet", "http"), "HttpServletRequest.java", _SERVLET_STUB),
        (("com", "acme"), "Dao.java", _SRC),
    ):
        d = os.path.join(root, *pkg)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
            fh.write(src)
    proc = subprocess.run(
        ["javac", "-g",
         os.path.join("javax", "servlet", "http", "HttpServletRequest.java"),
         os.path.join("com", "acme", "Dao.java")],
        cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"javac failed: {proc.stderr[:400]}")

    nodes = []
    for fi in discover(root):
        nodes.extend(_extract_one(fi, "t")[0])
    _edges, _synth, rep = resolve_java_bytecode(nodes, root, "t", java_files_seen=2)
    if not rep.available:
        pytest.skip(f"bytecode pass unavailable: {rep.reason}")
    _apply_taint_marks(nodes, rep, "t")
    return {n.name: n for n in nodes if n.label == "Function"}


class TestMarking:
    def test_the_vulnerable_method_is_marked_as_both_source_and_sink(self, marked):
        fn = marked.get("vulnerable")
        assert fn is not None, "fixture produced no Function node"
        assert fn.taint_source is True, "req.getParameter should mark the source"
        assert fn.taint_categories, "conn.createStatement/executeQuery should mark a sink"
        assert any("CWE-89" in k for k in fn.taint_categories), fn.taint_categories

    def test_a_method_with_neither_is_left_clean(self, marked):
        """The control. Marking everything is the same as marking nothing."""
        fn = marked.get("noTaintAtAll")
        assert fn is not None
        assert fn.taint_source is False
        assert fn.taint_categories == []
        assert fn.taint_sites == ""

    def test_call_sites_carry_real_lines_and_argument_positions(self, marked):
        """The whole point of call-site facts.

        A boolean says "this function has a sink somewhere". This must say which
        line and which argument, or a finding can only point at the declaration
        and five paths to one sink cannot be collapsed into one finding.
        """
        fn = marked["vulnerable"]
        assert fn.taint_sites, "no call sites recorded"
        sites = json.loads(fn.taint_sites)
        assert sites, "taint_sites decoded to nothing"
        for s in sites:
            assert s["line"] > 0, f"call site without a real line: {s}"
            assert s["role"] in ("source", "sink", "sanitizer")
        # The SQL sink must name argument 0 — the query string.
        sinks = [s for s in sites if s["role"] == "sink" and "CWE-89" in s["cat"]]
        assert sinks, f"no SQL sink recorded in {sites}"
        assert any(0 in s["args"] for s in sinks), (
            f"the query-string argument was not identified: {sinks}")

    def test_sites_are_sorted_so_reingest_is_stable(self, marked):
        """An unstable property value makes every node look changed on re-ingest,
        which defeats the incremental path."""
        sites = json.loads(marked["vulnerable"].taint_sites)
        assert [s["line"] for s in sites] == sorted(s["line"] for s in sites)

    def test_marks_are_cleared_from_the_report_after_use(self, marked):
        """Holding one entry per marked function inside a returned object across
        the derive memory peak is the mistake authoritative_override_methods made
        and had to have undone."""
        # marked fixture already ran _apply_taint_marks; re-deriving the report
        # here would be a second compile, so assert via the function it exposes.
        from graph_core.bytecode_resolver import BytecodeReport
        rep = BytecodeReport()
        rep.function_sink_kinds = {"a": {"CWE-89/sql-injection"}}
        rep.function_sources = {"a"}
        rep.function_sites = {"a": [(1, "CWE-89/sql-injection", "sink", (0,))]}
        _apply_taint_marks([], rep, "t")
        assert not rep.function_sink_kinds
        assert not rep.function_sources
        assert not rep.function_sites


class TestStaleBuildDegradesGracefully:
    """A stale compiled build must not destroy a run.

    models.py is Cython-compiled, and Node is a slots dataclass — so a new
    pipeline.so against an old models.so raises AttributeError on the first
    assignment, roughly 15 minutes into a build, after extraction is already
    paid for. Taint marks are an annotation; the graph is complete without them.
    """

    class _OldNode:
        """A Node from before the taint fields existed."""
        __slots__ = ("id", "label")

        def __init__(self, nid):
            self.id = nid
            self.label = "Function"

    def test_missing_fields_skip_marking_instead_of_raising(self):
        from graph_core.bytecode_resolver import BytecodeReport
        rep = BytecodeReport()
        rep.function_sink_kinds = {"a": {"CWE-89/sql-injection"}}
        rep.function_sources = {"a"}
        rep.function_sites = {"a": [(1, "CWE-89/sql-injection", "sink", (0,))]}
        old = [self._OldNode("a")]
        _apply_taint_marks(old, rep, "t")   # must not raise
        assert not rep.function_sink_kinds, "marks should be dropped, not retained"

    def test_a_current_node_still_gets_marked(self):
        """The guard must not fire on a healthy build."""
        from graph_core.bytecode_resolver import BytecodeReport
        from graph_core.models import Node
        rep = BytecodeReport()
        rep.function_sink_kinds = {"a": {"CWE-89/sql-injection"}}
        rep.function_sources = set()
        rep.function_sites = {}
        n = Node(id="a", label="Function", name="f", fqn="f", repo="t")
        _apply_taint_marks([n], rep, "t")
        assert n.taint_categories == ["CWE-89/sql-injection"]

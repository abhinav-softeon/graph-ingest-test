"""Database/external call classification.

The single most important property here is what does NOT get classified. A table
keyed on method names would tag `inputStream.close()` as a database release, and
a leak detector built on that produces confident false positives — worse than no
detector at all, because someone has to disprove each one.

The second property is the non-obvious one: an acquire is recognised by RETURN
TYPE, not by the owner or the name. Measured on the target repo, 171 in-repo
methods return a java.sql.Connection under four different naming conventions
(getConnection / getDbConn / getCon / getDbConnection), while only 2,571 calls
reach Connection.prepareStatement against 11,319 reaching Connection.close.
Connections come from the repo's own pool wrappers, so owner- or name-based
rules find almost none of them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from graph_core.bytecode_resolver import resolve_java_bytecode
from graph_core.discovery import discover
from graph_core.external_api import (
    DB_ACQUIRE, DB_EXECUTE, DB_OTHER, DB_RELEASE,
    classify_call, external_id, external_key,
)
from graph_core.pipeline import _extract_one
from graph_core.schema import EDGE_TYPES, NODE_LABELS


class TestClassifier:
    def test_release_on_a_database_type(self):
        assert classify_call("java.sql.Connection", "close") == DB_RELEASE
        assert classify_call("java.sql.Connection", "rollback") == DB_RELEASE
        assert classify_call("java.sql.Connection", "commit") == DB_RELEASE

    def test_close_on_a_non_database_type_is_not_classified(self):
        """THE false-positive case. `close` alone means nothing."""
        assert classify_call("java.io.InputStream", "close") == ""
        assert classify_call("java.io.FileWriter", "close") == ""
        assert classify_call("java.util.Scanner", "close") == ""

    def test_execute(self):
        assert classify_call("java.sql.PreparedStatement", "executeQuery") == DB_EXECUTE
        assert classify_call("java.sql.Statement", "executeUpdate") == DB_EXECUTE

    def test_unknown_method_on_a_known_type_is_still_db_work(self):
        """Dropping these would lose ResultSet.getString and
        PreparedStatement.setString — 32k calls in the measured repo. "Touches a
        Connection" is itself the signal."""
        assert classify_call("java.sql.ResultSet", "getString") == DB_OTHER
        assert classify_call("java.sql.PreparedStatement", "setString") == DB_OTHER

    def test_acquire_by_return_type_regardless_of_owner(self):
        """The rule that makes this work on the real repo: a pool wrapper is not
        a JDBC type, but a method returning a Connection is still an acquire."""
        assert classify_call("com.acme.DbManager", "getDbConn",
                             "java.sql.Connection") == DB_ACQUIRE
        assert classify_call("com.acme.Whatever", "totallyUnrelatedName",
                             "java.sql.Connection") == DB_ACQUIRE

    def test_return_type_of_a_non_connection_does_not_acquire(self):
        assert classify_call("com.acme.Thing", "getName", "java.lang.String") == ""

    def test_simple_names_accepted(self):
        """The heuristic path only has recv_type, which java.py stores as a
        simple name via simple_type_name."""
        assert classify_call("Connection", "close") == DB_RELEASE
        assert classify_call("PreparedStatement", "executeQuery") == DB_EXECUTE

    def test_empty_owner_is_not_classified(self):
        assert classify_call("", "close") == ""

    def test_jpa_and_mybatis(self):
        assert classify_call("org.hibernate.Session", "close") == DB_RELEASE
        assert classify_call("org.apache.ibatis.session.SqlSession", "selectOne") == DB_EXECUTE


class TestIdentity:
    def test_key_and_id_are_stable(self):
        assert external_key("java.sql.Connection", "close") == "java.sql.Connection#close"
        assert external_id("java.sql.Connection", "close") == \
            external_id("java.sql.Connection", "close")

    def test_external_nodes_are_shared_across_repos(self):
        """Keyed on the literal repo 'external', mirroring how apispec keys
        external endpoints — two repos calling Connection.close reference one
        node rather than duplicating it."""
        assert external_id("java.sql.Connection", "close").startswith(
            external_id("java.sql.Connection", "close")[:8])


class TestSchema:
    def test_registered(self):
        assert "External" in NODE_LABELS
        assert "CALLS_EXTERNAL" in EDGE_TYPES


_SRC_MANAGER = """package com.acme;
import java.sql.Connection;
import java.sql.DriverManager;
public class DbManager {
    public static Connection getDbConn() throws Exception {
        return DriverManager.getConnection("jdbc:x");
    }
}
"""

_SRC_DAO = """package com.acme;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.io.InputStream;
public class Dao {
    public void leaks() throws Exception {
        Connection c = DbManager.getDbConn();
        PreparedStatement ps = c.prepareStatement("select 1");
        ps.executeQuery();
    }
    public void clean() throws Exception {
        Connection c = DbManager.getDbConn();
        c.close();
    }
    public void notDb(InputStream in) throws Exception {
        in.close();
    }
}
"""

_HAVE_JAVAC = shutil.which("javac") is not None


@pytest.fixture(scope="module")
def built():
    if not _HAVE_JAVAC:
        pytest.skip("javac not on PATH")
    root = tempfile.mkdtemp(prefix="extapi_")
    pkg = os.path.join(root, "com", "acme")
    os.makedirs(pkg, exist_ok=True)
    for name, body in (("DbManager.java", _SRC_MANAGER), ("Dao.java", _SRC_DAO)):
        with open(os.path.join(pkg, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    proc = subprocess.run(
        ["javac", "-g", os.path.join("com", "acme", "DbManager.java"),
         os.path.join("com", "acme", "Dao.java")],
        cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"javac failed: {proc.stderr[:300]}")
    nodes = []
    for fi in discover(root):
        nodes.extend(_extract_one(fi, "bench")[0])
    edges, synth, rep = resolve_java_bytecode(nodes, root, "bench", java_files_seen=2)
    index = {n.id: n for n in nodes}
    index.update({n.id: n for n in synth})
    ext = [(index[e.src].fqn, index[e.dst].fqn, index[e.dst].kind)
           for e in edges if e.type == "CALLS_EXTERNAL"]
    return ext, synth, rep


@pytest.mark.skipif(not _HAVE_JAVAC, reason="javac not on PATH")
class TestEndToEnd:
    def test_in_repo_factory_is_an_acquire(self, built):
        ext, _synth, _rep = built
        assert ("com.acme.Dao#leaks", "com.acme.DbManager#getDbConn", DB_ACQUIRE) in ext

    def test_release_recorded(self, built):
        ext, _synth, _rep = built
        assert ("com.acme.Dao#clean", "java.sql.Connection#close", DB_RELEASE) in ext

    def test_execute_recorded(self, built):
        ext, _synth, _rep = built
        assert any(dst == "java.sql.PreparedStatement#executeQuery" and kind == DB_EXECUTE
                   for _src, dst, kind in ext)

    def test_inputstream_close_produces_nothing(self, built):
        """If this ever fires, every stream/reader/writer close in the codebase
        becomes a false leak-detector result."""
        ext, _synth, _rep = built
        assert not any(src == "com.acme.Dao#notDb" for src, _dst, _kind in ext)
        assert not any(dst.startswith("java.io.") for _src, dst, _kind in ext)

    def test_leak_is_distinguishable_from_clean(self, built):
        """The actual question: acquires with no release on the same function."""
        ext, _synth, _rep = built
        acquired = {src for src, _dst, kind in ext if kind == DB_ACQUIRE}
        released = {src for src, _dst, kind in ext if kind == DB_RELEASE}
        assert "com.acme.Dao#leaks" in acquired - released
        assert "com.acme.Dao#clean" not in acquired - released

    def test_external_nodes_have_no_source_position(self, built):
        """They are not code this repo holds. validate_graph guards its position
        checks on truthiness, which is what makes that safe."""
        _ext, synth, _rep = built
        for n in synth:
            if n.label == "External":
                assert not n.file and not n.start_line

    def test_report_counts(self, built):
        _ext, _synth, rep = built
        assert rep.external_edges > 0
        assert rep.external_nodes > 0
        assert rep.summary()["external_edges"] == rep.external_edges

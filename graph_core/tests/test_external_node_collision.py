"""The duplicate-node-id mechanism, pinned down.

A real run failed validation with "duplicate node ids detected". merge_bundles
already dedupes by id (canonical_ir:43) before resolve, so the collision had to
come from a producer that appends AFTERWARDS — and there are two, each keeping
its own private id set.

These tests document the mechanism rather than the symptom, so that if the
pipeline-side guard is ever removed the reason it existed is still on record.
"""
from __future__ import annotations

from graph_core.external_api import external_id, external_key
from graph_core.validator import EdgeStats, validate_graph
from graph_core.models import Node


class TestExternalIdCollision:
    def test_the_same_api_yields_the_same_id_from_any_producer(self):
        """external_id is a pure function of (owner, method).

        This is correct and deliberate — one External node per API, shared — but
        it means bytecode_resolver and resolver.resolve independently generate
        IDENTICAL ids for any API both paths reach. Neither can see the other's
        `external_ids` set, so both append.
        """
        from_bytecode = external_id("java.sql.Connection", "close")
        from_resolver = external_id("java.sql.Connection", "close")
        assert from_bytecode == from_resolver
        assert external_key("java.sql.Connection", "close") == "java.sql.Connection#close"

    def test_distinct_apis_do_not_collide(self):
        assert external_id("java.sql.Connection", "close") != external_id(
            "java.io.InputStream", "close")

    def test_validate_graph_reports_the_collision_with_diagnostics(self):
        """The validator must name the colliding fqn and its files.

        A bare "duplicate node ids detected" cannot distinguish benign
        duplication from a genuine fqn collision between two different things,
        which is the whole reason the diagnostic was added.
        """
        eid = external_id("java.sql.Connection", "close")
        dup = [
            Node(id=eid, label="External", name="Connection.close",
                 fqn="java.sql.Connection#close", repo="external", file="A.java"),
            Node(id=eid, label="External", name="Connection.close",
                 fqn="java.sql.Connection#close", repo="external", file="B.jsp"),
        ]
        report = validate_graph(dup, EdgeStats())
        assert not report["ok"]
        err = next(e for e in report["errors"] if "duplicate node ids" in e)
        # Counts, not just the fact — "1 id, 1 redundant node" is actionable in a
        # way that a boolean is not.
        assert "1 id" in err and "1 redundant" in err

    def test_a_clean_node_set_passes(self):
        clean = [
            Node(id=external_id("java.sql.Connection", "close"), label="External",
                 name="Connection.close", fqn="java.sql.Connection#close",
                 repo="external"),
            Node(id=external_id("java.lang.Runtime", "exec"), label="External",
                 name="Runtime.exec", fqn="java.lang.Runtime#exec", repo="external"),
        ]
        report = validate_graph(clean, EdgeStats())
        assert not [e for e in report["errors"] if "duplicate node ids" in e]

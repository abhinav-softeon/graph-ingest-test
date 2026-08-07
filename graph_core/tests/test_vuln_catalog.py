"""Vulnerability catalog lookup.

Weighted toward NEGATIVE tests, because the catalog's failure mode is
over-matching. A table that recognises everything is worthless in the same way a
name-matching resolver is: it produces confident answers about calls it cannot
actually distinguish. The `close`/`write`/`execute` cases below are the specific
collisions external_api.py's design note warns about.
"""
from __future__ import annotations

from graph_core import catalog
from graph_core.catalog import ANY_ARG, RECEIVER, SANITIZER, SINK, SOURCE


class TestSources:
    def test_servlet_get_parameter_is_a_source(self):
        hit = catalog.classify_taint(
            "javax.servlet.http.HttpServletRequest", "getParameter")
        assert hit is not None, "the primary untrusted-input entry point"
        entry, args = hit
        assert entry.role == SOURCE
        assert args == (RECEIVER,)

    def test_jakarta_spelling_resolves_to_the_same_entry(self):
        old = catalog.classify_taint(
            "javax.servlet.http.HttpServletRequest", "getHeader")
        new = catalog.classify_taint(
            "jakarta.servlet.http.HttpServletRequest", "getHeader")
        assert old is not None and new is not None
        # Same object, not merely an equal copy — the two spellings must not be
        # able to drift apart as entries are edited.
        assert old[0] is new[0]

    def test_method_match_is_case_insensitive(self):
        for spelling in ("getParameter", "getparameter", "GETPARAMETER"):
            assert catalog.classify_taint(
                "javax.servlet.http.HttpServletRequest", spelling) is not None

    def test_uncatalogued_method_on_a_catalogued_owner_does_not_match(self):
        # getContextPath is not attacker-controlled. An owner being catalogued
        # must not make its whole surface a source.
        assert catalog.classify_taint(
            "javax.servlet.http.HttpServletRequest", "getContextPath") is None


class TestSinks:
    def test_runtime_exec_is_a_command_injection_sink(self):
        hit = catalog.classify_taint("java.lang.Runtime", "exec")
        assert hit is not None
        entry, args = hit
        assert entry.role == SINK
        assert entry.category == "CWE-78/command-injection"
        # Positions are the UNION ACROSS OVERLOADS, because entries are keyed by
        # method NAME and `exec` has six. Position 0 is the command in every
        # overload; position 1 is the environment array in exec(String,String[])
        # and exec(String[],String[]), which upstream also treats as dangerous.
        #
        # This is an over-approximation and a real limitation: on the 1-argument
        # exec(String) there is no position 1 at all. Harmless for a consumer
        # asking "does taint reach argument 1" (there is none to reach), but it
        # means the table cannot distinguish overloads. Keying by descriptor would
        # fix it and the bytecode path already carries one — the miner currently
        # discards it. Recorded here rather than in prose so the day it matters,
        # this test is where the decision is written down.
        assert args == (0, 1)

    def test_constructor_is_addressable(self):
        hit = catalog.classify_taint("java.io.FileInputStream", "<init>")
        assert hit is not None
        assert hit[1] == (0,)

    def test_sink_with_no_argument_uses_any_arg(self):
        # The taint is the stream bound earlier, so there is no argument index to
        # report. ANY_ARG is how that is expressed, and it must not be confused
        # with "argument 0".
        hit = catalog.classify_taint("java.io.ObjectInputStream", "readObject")
        assert hit is not None
        assert hit[1] == (ANY_ARG,)
        assert ANY_ARG != 0


class TestSanitizers:
    def test_prepared_statement_setter_sanitizes_the_VALUE_not_the_index(self):
        """The whole reason argument positions are stored.

        setString(int parameterIndex, String value) — position 1 is the bound
        value, position 0 is the placeholder number. An entry that claimed
        position 0 would report the index as sanitized and the actual data as
        unsanitized, i.e. exactly backwards.
        """
        hit = catalog.classify_taint("java.sql.PreparedStatement", "setString")
        assert hit is not None
        entry, args = hit
        assert entry.role == SANITIZER
        assert args == (1,)
        assert 0 not in args

    def test_prepare_statement_text_is_a_sink_while_its_setters_sanitize(self):
        """Both halves of the JDBC story must be present.

        Without the sink, concatenated SQL is missed. Without the sanitizer,
        every correctly parameterized query is a false positive.
        """
        sink = catalog.classify_taint("java.sql.Connection", "prepareStatement")
        san = catalog.classify_taint("java.sql.PreparedStatement", "setInt")
        assert sink is not None and sink[0].role == SINK
        assert san is not None and san[0].role == SANITIZER


class TestOwnerFirstDiscipline:
    """The property the catalog is worthless without.

    Every case here is a real collision on a bare method name, and each one
    would be a confident wrong answer in a method-keyed table.
    """

    def test_close_on_a_stream_is_not_catalogued(self):
        assert catalog.classify_taint("java.io.InputStream", "close") is None

    def test_write_on_an_uncatalogued_writer_is_not_an_xss_sink(self):
        # A logger, a file writer, a socket — `write` alone means nothing.
        assert catalog.classify_taint("java.io.FileWriter", "write") is None
        assert catalog.classify_taint("org.slf4j.Logger", "write") is None

    def test_execute_on_an_executor_is_not_a_sql_sink(self):
        assert catalog.classify_taint(
            "java.util.concurrent.ThreadPoolExecutor", "execute") is None

    def test_repo_own_type_sharing_a_simple_name_DOES_match_today(self):
        # A codebase's own `Statement`/`Cipher`/`File` class must not inherit the
        # JDK entry just by being called the same thing. Fully-qualified lookup
        # is the guard; the simple-name index only holds unambiguous names, and
        # it is only consulted when the FQN itself missed.
        assert catalog.classify_taint(
            "com.softeon.scm.app.objects.Statement", "executeQuery") is not None, (
            "documents CURRENT behavior: an in-repo type whose simple name "
            "collides with a catalogued JDK type DOES fall through to the "
            "simple-name index. Acceptable only because bytecode supplies real "
            "FQNs for 99.98% of Java here; revisit if the heuristic path grows."
        )

    def test_unknown_owner_never_matches(self):
        assert catalog.classify_taint("com.acme.Whatever", "getParameter") is None

    def test_empty_input_is_safe(self):
        assert catalog.classify_taint("", "exec") is None
        assert catalog.classify_taint("java.lang.Runtime", "") is None


class TestSimpleNameIndex:
    def test_unambiguous_simple_name_resolves(self):
        hit = catalog.classify_taint("Runtime", "exec")
        assert hit is not None
        assert hit[0].category == "CWE-78/command-injection"

    def test_ambiguous_simple_name_is_excluded(self):
        """`StringEscapeUtils` exists under both commons-lang and commons-text
        with DIFFERENT method sets, so the simple name must not resolve to
        either — picking one would silently apply the wrong method table."""
        assert "StringEscapeUtils" not in catalog.JAVA_BY_SIMPLE_NAME
        assert catalog.classify_taint("StringEscapeUtils", "escapeHtml") is None
        # ...while both fully-qualified spellings still work.
        assert catalog.classify_taint(
            "org.apache.commons.lang.StringEscapeUtils", "escapeHtml") is not None
        assert catalog.classify_taint(
            "org.apache.commons.text.StringEscapeUtils", "escapeHtml4") is not None


class TestCoverageReporting:
    def test_all_three_roles_are_populated(self):
        """A catalog missing any role is broken in a specific way: no sources
        means nothing to seed from, no sinks means nothing to report, no
        sanitizers means everything correct is reported anyway."""
        by_role = catalog.stats()["by_role"]
        for role in (SOURCE, SINK, SANITIZER):
            assert by_role.get(role, 0) > 0, f"no {role} entries"

    def test_servlet_request_is_no_longer_inert(self):
        """This test used to assert the OPPOSITE, and that was the point.

        It was written as `assert HttpServletRequest in missing_edge_coverage()`
        to pin the integration gap: external_api could not classify servlet
        types, so every taint source in a servlet app produced no
        CALLS_EXTERNAL edge and the catalog entry was inert. The note said it
        would fail and be rewritten when the gap closed. It closed — the catalog
        now feeds external_api and GRAPH_CATALOG_EXTERNAL defaults to
        `recommended` — so the assertion is inverted rather than deleted, to keep
        a regression guard on the thing that was broken.
        """
        assert "javax.servlet.http.HttpServletRequest" not in (
            catalog.missing_edge_coverage())

    def test_jdbc_execute_entries_are_already_live(self):
        """The one group that needs no external_api work: external_api already
        classifies the JDBC execute family as db_execute, so those edges exist."""
        assert "java.sql.Statement" not in catalog.missing_edge_coverage()
        assert "java.sql.PreparedStatement" not in catalog.missing_edge_coverage()

    def test_stats_are_self_consistent(self):
        s = catalog.stats()
        assert s["owners"] >= s["distinct_entries"]  # javax/jakarta aliases
        assert s["signatures"] > s["owners"]
        assert 0 <= s["owners_needing_external_api"] <= s["owners"]


class TestExternalApiIntegration:
    """The catalog feeding external_api.classify_call.

    This is what makes catalogued entries reachable: without a CALLS_EXTERNAL
    edge there is nothing in the graph for a lookup to attach to, so an entry
    that external_api cannot classify is known and unusable.
    """

    @staticmethod
    def _set(monkeypatch, value):
        from graph_core import external_api as ea
        if value is None:
            monkeypatch.delenv("GRAPH_CATALOG_EXTERNAL", raising=False)
        else:
            monkeypatch.setenv("GRAPH_CATALOG_EXTERNAL", value)
        ea._reset_enabled_categories()
        return ea

    def test_default_is_recommended_so_sources_exist_out_of_the_box(self, monkeypatch):
        """The default was `off` while the integration was unproven, on the
        argument that a clean baseline made the edge-count delta attributable.
        That baseline already exists (run c1d0c433), so defaulting to off only
        meant a fresh run would exercise none of this and produce a graph with no
        taint sources in it at all.
        """
        ea = self._set(monkeypatch, None)
        assert ea.classify_call(
            "javax.servlet.http.HttpServletRequest",
            "getParameter") == ea.TAINT_SOURCE
        assert ea.classify_call("java.lang.Runtime", "exec") == "exec"

    def test_off_is_still_reachable_for_a_strict_before_after(self, monkeypatch):
        ea = self._set(monkeypatch, "off")
        assert ea.classify_call(
            "javax.servlet.http.HttpServletRequest", "getParameter") == ""
        assert ea.classify_call("java.lang.Runtime", "exec") == ""
        # ...and the pre-catalog database behaviour is untouched either way.
        assert ea.classify_call("java.sql.Statement", "executeQuery") == "db_execute"

    def test_database_classification_is_untouched_when_off(self, monkeypatch):
        ea = self._set(monkeypatch, "off")
        assert ea.classify_call("java.sql.Statement", "executeQuery") == "db_execute"

    def test_recommended_enables_sources_and_sinks(self, monkeypatch):
        ea = self._set(monkeypatch, "recommended")
        assert ea.classify_call(
            "javax.servlet.http.HttpServletRequest",
            "getParameter") == ea.TAINT_SOURCE
        # "exec", not TAINT_SINK — sinks map onto reach.py's existing seed
        # vocabulary; see test_sink_kinds_speak_the_vocabulary...
        assert ea.classify_call("java.lang.Runtime", "exec") == "exec"

    def test_recommended_excludes_the_high_volume_low_value_classes(self, monkeypatch):
        """String.format is a real mined sink and is still excluded by default.

        It is among the most frequent calls in a Java codebase and reports mostly
        on log statements, so enabling it would add enormous edge volume for
        little yield. `all` turns it on for anyone who wants it.
        """
        ea = self._set(monkeypatch, "recommended")
        assert ea.classify_call("java.lang.String", "format") == ""
        ea = self._set(monkeypatch, "all")
        assert ea.classify_call("java.lang.String", "format") == ea.TAINT_SINK

    def test_explicit_category_list_enables_only_that_category(self, monkeypatch):
        ea = self._set(monkeypatch, "CWE-78/command-injection")
        assert ea.classify_call("java.lang.Runtime", "exec") == "exec"
        assert ea.classify_call(
            "javax.servlet.http.HttpServletRequest", "getParameter") == ""

    def test_resource_kinds_still_win_over_catalog_roles(self, monkeypatch):
        """Ordering matters and is asserted.

        java.sql.Statement is BOTH a mined injection sink and a known database
        type. The database answer must survive, because the acquire/execute/
        release vocabulary drives the resource-leak analysis and the catalog does
        not express it.
        """
        ea = self._set(monkeypatch, "all")
        assert ea.classify_call("java.sql.Statement", "executeQuery") == "db_execute"
        assert ea.classify_call("java.sql.Connection", "close") == "db_release"

    def test_owner_first_discipline_survives_the_integration(self, monkeypatch):
        ea = self._set(monkeypatch, "all")
        assert ea.classify_call("java.io.InputStream", "close") == ""
        assert ea.classify_call("java.io.FileWriter", "write") == ""

    def test_enabling_the_catalog_closes_the_inert_gap(self, monkeypatch):
        """missing_edge_coverage() should collapse once the catalog is wired in.

        With the setting off, essentially every catalogued owner is inert — that
        is the finding this whole integration exists to fix, so it is asserted in
        both directions rather than described.
        """
        self._set(monkeypatch, "off")
        off = len(catalog.missing_edge_coverage())
        self._set(monkeypatch, "all")
        on = len(catalog.missing_edge_coverage())
        assert off > 60, "expected nearly every owner inert when off"
        assert on == 0, f"expected full coverage when on, {on} still inert"

    def test_sink_kinds_speak_the_vocabulary_reach_py_already_seeds_from(
            self, monkeypatch):
        """Cross-module contract, asserted because it silently rotted once.

        analysis/reach.py seeds its reachability closure from DANGEROUS_KINDS,
        four of which — exec, file_write, deserialize, response — were DEAD:
        external_api never emitted them, so the analysis was written for sinks
        nothing produced. The catalog covers exactly those categories, so it maps
        onto the existing words instead of minting new ones.

        If this fails, either reach.py's vocabulary changed or a category lost its
        mapping — and the symptom would otherwise be silent: reachability simply
        finding nothing, which looks identical to a clean codebase.
        """
        from analysis.reach import DANGEROUS_KINDS
        ea = self._set(monkeypatch, "recommended")
        expected = {
            ("java.lang.Runtime", "exec"): "exec",
            ("javax.script.ScriptEngine", "eval"): "exec",
            ("java.io.FileInputStream", "<init>"): "file_write",
            ("java.io.ObjectInputStream", "readObject"): "deserialize",
            ("java.io.PrintWriter", "println"): "response",
            ("javax.servlet.http.HttpServletResponse", "addHeader"): "response",
        }
        for (owner, method), kind in expected.items():
            got = ea.classify_call(owner, method)
            assert got == kind, f"{owner}.{method}: expected {kind}, got {got!r}"
            assert kind in DANGEROUS_KINDS, (
                f"{kind} is not in reach.py's seed set — the mapping points at a "
                f"word the analysis layer does not consume")

    def test_sources_and_sanitizers_keep_the_generic_taint_kinds(self, monkeypatch):
        """reach.py seeds ENTRY POINTS from annotations, not External nodes, so
        there is no existing word for a source to map onto."""
        ea = self._set(monkeypatch, "recommended")
        assert ea.classify_call(
            "javax.servlet.http.HttpServletRequest",
            "getParameter") == ea.TAINT_SOURCE
        assert ea.classify_call(
            "java.sql.PreparedStatement", "setString") in (
                ea.TAINT_SANITIZER, "db_other"), (
            "PreparedStatement is a known DB type, so the resource classifier "
            "answers first — documented in test_resource_kinds_still_win")

    def test_second_order_source_beats_the_generic_db_fallback(self, monkeypatch):
        """ResultSet.getString is database work AND a source of untrusted data.

        Stored data read back and concatenated into the next query is the classic
        second-order chain, and it is invisible to a catalog that only knows HTTP
        entry points. Classifying it db_other is not wrong, just the less useful
        of two true answers — and db_other is in reach.py's DANGEROUS_KINDS, so it
        would mark every ResultSet read as reaching a SINK, which is backwards for
        something that is a SOURCE.
        """
        ea = self._set(monkeypatch, "recommended")
        assert ea.classify_call("java.sql.ResultSet", "getString") == ea.TAINT_SOURCE

    def test_specific_db_kinds_still_outrank_the_catalog(self, monkeypatch):
        """Only the DB_OTHER fallback yields. The specific kinds drive the
        resource-leak analysis (paths.py anchors on db_acquire) and must not be
        displaced by a catalog role."""
        ea = self._set(monkeypatch, "recommended")
        assert ea.classify_call("java.sql.ResultSet", "close") == "db_release"
        assert ea.classify_call("java.sql.Statement", "executeQuery") == "db_execute"
        assert ea.classify_call("java.sql.Connection", "getConnection") == "db_acquire"

    def test_non_payload_result_set_getters_are_not_sources(self, monkeypatch):
        """Precision guard. Numeric/cursor/metadata methods cannot carry an
        injection payload, so marking them would be pure false-positive volume."""
        ea = self._set(monkeypatch, "recommended")
        for method in ("getInt", "getLong", "getDate", "next", "wasNull",
                       "findColumn", "getMetaData"):
            got = ea.classify_call("java.sql.ResultSet", method)
            assert got != ea.TAINT_SOURCE, f"{method} should not be a source"


class TestPropagators:
    """How taint MOVES. Separate from roles, deliberately."""

    def test_string_builder_append_propagates_arg_and_receiver(self):
        """The single most important propagator rule for Java injection.

        `"SELECT ... " + request.getParameter("id")` compiles to a
        StringBuilder.append chain. The result must be tainted if EITHER the
        appended argument or the builder already was — receiver propagation is
        what makes a chain of appends accumulate taint rather than losing it at
        every link.
        """
        pos = catalog.classify_propagator("java.lang.StringBuilder", "append")
        assert pos is not None, "without this a DFG loses taint at the first hop"
        assert 0 in pos, "the appended argument"
        assert RECEIVER in pos, "the builder's existing taint carries through"

    def test_string_buffer_too(self):
        # Legacy Java concatenation compiles to StringBuffer in older targets.
        assert catalog.classify_propagator("java.lang.StringBuffer", "append")

    def test_propagators_are_not_roles(self):
        """append must NOT answer classify_taint.

        Folding propagation into the role table would make every string
        concatenation report as a dangerous call — the owner-first discipline's
        failure mode, arrived at from a different direction.
        """
        assert catalog.classify_taint("java.lang.StringBuilder", "append") is None

    def test_uncatalogued_method_returns_none(self):
        assert catalog.classify_propagator("java.lang.StringBuilder", "reverse2") is None
        assert catalog.classify_propagator("com.acme.Thing", "append") is None


class TestTypeCoercionSanitizers:
    """The highest-yield sanitizer class, and one no upstream rule set encodes.

    FindSecBugs does not mark these because in its model a primitive is never
    tainted, so there is nothing to say. That is sound for their engine and
    useless for a catalog read by an LLM or a path walker, which sees
    parseInt(taintedString) and needs to be told the chain ends there.
    """

    def test_parse_int_sanitizes_its_string_argument(self):
        hit = catalog.classify_taint("java.lang.Integer", "parseInt")
        assert hit is not None, (
            "parseInt(request.getParameter(...)) is provably safe — an int "
            "cannot carry a SQL payload — and this is the single largest "
            "false-positive class in a legacy app")
        entry, args = hit
        assert entry.role == SANITIZER
        assert args == (0,)

    def test_the_whole_numeric_parse_family_is_covered(self):
        for owner, method in [
            ("java.lang.Long", "parseLong"),
            ("java.lang.Short", "parseShort"),
            ("java.lang.Byte", "parseByte"),
            ("java.lang.Double", "parseDouble"),
            ("java.lang.Float", "parseFloat"),
            ("java.lang.Boolean", "parseBoolean"),
            ("java.util.UUID", "fromString"),
            ("java.math.BigDecimal", "<init>"),
        ]:
            hit = catalog.classify_taint(owner, method)
            assert hit is not None and hit[0].role == SANITIZER, f"{owner}.{method}"

    def test_context_encoders_are_present(self):
        for owner, method in [
            ("java.net.URLEncoder", "encode"),
            ("org.owasp.encoder.Encode", "forHtml"),
            ("org.springframework.web.util.HtmlUtils", "htmlEscape"),
        ]:
            hit = catalog.classify_taint(owner, method)
            assert hit is not None and hit[0].role == SANITIZER, f"{owner}.{method}"

    def test_unrelated_methods_on_a_sanitizer_owner_do_not_match(self):
        """Integer is catalogued; that must not make all of Integer a sanitizer."""
        assert catalog.classify_taint("java.lang.Integer", "intValue") is None
        assert catalog.classify_taint("java.lang.Integer", "compareTo") is None

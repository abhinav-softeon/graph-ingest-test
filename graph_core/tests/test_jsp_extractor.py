"""JSP extraction guardrails.

The design risk here is the translation layer: JSP becomes synthetic Java, so a
bug does not crash — it silently reports the wrong line, or leaks scaffolding
this module invented into the graph as if the page's author had written it.

Both failure modes are asserted directly:
  * every reported position must be a real .jsp line
  * nothing synthetic (the print wrapper, the class braces) may escape
"""
from __future__ import annotations

import os
import tempfile

from graph_core.artifacts import is_artifact
from graph_core.discovery import EXT_LANG, discover
from graph_core.extractors import extract
from graph_core.extractors.jsp import _OUT_SENTINEL, _class_identity, translate

_PAGE = """<%@ page import="com.acme.OrderService" %>
<%@ taglib uri="http://java.sun.com/jsp/jstl/core" prefix="c" %>
<%@ include file="header.jspf" %>
<html><body>
<%!
  private int hitCount = 0;
%>
<jsp:useBean id="cart" class="com.acme.Cart" scope="session"/>
<%
  OrderService svc = new OrderService();
  String id = request.getParameter("id");
  hitCount++;
  if (id != null) {
%>
  <p>Order: <%= svc.describe(id) %></p>
  <p>Total: ${cart.total}</p>
<%
  }
  cart.checkout();
%>
<jsp:forward page="/done.jsp"/>
</body></html>
"""


def _write(name: str, body: str) -> str:
    root = tempfile.mkdtemp(prefix="jsp_")
    path = os.path.join(root, *name.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return root


def _extract_one(name: str = "WEB-INF/views/order.jsp", body: str = _PAGE):
    root = _write(name, body)
    files = discover(root)
    assert len(files) == 1, f"expected one JSP, got {[f.relpath for f in files]}"
    return (*extract(files[0], "bench"), files[0])


class TestRegistration:
    def test_jsp_is_source_not_artifact(self):
        """Both tables must agree: EXT_LANG parses it, so artifacts must not
        claim it — _source_lang refuses anything artifacts.py owns."""
        for ext in (".jsp", ".jspf", ".tag"):
            assert EXT_LANG.get(ext) == "jsp"
            assert not is_artifact("page" + ext)

    def test_tld_stays_an_artifact(self):
        """A tag library descriptor is configuration, not code."""
        assert is_artifact("c.tld")


class TestTranslation:
    def test_split_scriptlet_block_reassembles(self):
        """`<% if (x) { %> html <% } %>` is two fragments that only form valid
        Java once concatenated in page order — exactly what Jasper does."""
        tr = translate(_PAGE, "WEB-INF/views/order.jsp")
        assert "if (id != null) {" in tr.java_source
        assert tr.java_source.count("}") >= 2

    def test_directives_collected(self):
        tr = translate(_PAGE, "a/order.jsp")
        assert ("com.acme.OrderService", 1) in tr.imports
        assert ("header.jspf", 3, "static") in tr.includes
        assert ("/done.jsp", 21, "forward") in tr.includes
        assert ("cart", "com.acme.Cart", 8) in tr.beans
        assert tr.taglibs and tr.taglibs[0][0] == "c"
        assert tr.el_expressions == 1

    def test_line_map_is_total(self):
        """Every synthetic line needs an entry or remapping has to guess."""
        tr = translate(_PAGE, "a/order.jsp")
        for lineno in range(1, len(tr.java_source.split("\n")) + 1):
            assert lineno in tr.line_map

    def test_class_identity_sanitizes_paths(self):
        """`WEB-INF` is not a legal Java identifier, and two pages both named
        index.jsp in different folders must not collide."""
        assert _class_identity("WEB-INF/views/order.jsp") == ("WEB_INF.views", "order_jsp")
        assert _class_identity("a/index.jsp")[1] == "index_jsp"
        assert _class_identity("a/index.jsp")[0] != _class_identity("b/index.jsp")[0]

    def test_leading_digit_is_legalised(self):
        assert _class_identity("404.jsp")[1] == "_404_jsp"


class TestExtraction:
    def test_nodes_span_the_real_page(self):
        nodes, _edges, _refs, fi = _extract_one()
        jsp_lines = fi.source.decode().count("\n") + 1
        page = {n.label: n for n in nodes}
        assert page["File"].start_line == 1
        assert page["File"].end_line == jsp_lines
        # The page class and its service method ARE the page; without this they
        # collapse onto the synthetic braces at line 1.
        assert page["Class"].end_line == jsp_lines
        fn = [n for n in nodes if n.label == "Function"][0]
        assert (fn.start_line, fn.end_line) == (1, jsp_lines)

    def test_every_position_is_inside_the_page(self):
        """A remapping bug shows up as a line number past the end of the file —
        which points a reader at nothing."""
        nodes, edges, refs, fi = _extract_one()
        jsp_lines = fi.source.decode().count("\n") + 1
        for n in nodes:
            assert 1 <= n.start_line <= jsp_lines, f"{n.fqn} start {n.start_line}"
            assert n.start_line <= n.end_line <= jsp_lines, f"{n.fqn} end {n.end_line}"
        for r in refs:
            assert 1 <= r.ref_line <= jsp_lines, f"{r.target_name} at {r.ref_line}"

    def test_calls_carry_receiver_types(self):
        """The point of declaring useBean as a typed local: `cart.checkout()`
        gets a recv_type, which is what turns off the resolver's fan-out."""
        _nodes, _edges, refs, _fi = _extract_one()
        calls = {r.target_name: r for r in refs if r.type == "CALLS"}
        assert calls["describe"].recv_type == "OrderService"
        assert calls["checkout"].recv_type == "Cart"

    def test_expression_call_reported_at_its_jsp_line(self):
        """`<%= svc.describe(id) %>` sits on line 15 of the page."""
        _nodes, _edges, refs, _fi = _extract_one()
        describe = [r for r in refs if r.target_name == "describe"][0]
        assert describe.ref_line == 15

    def test_scaffolding_does_not_leak(self):
        """The print wrapper exists only to make `<%= %>` parse. If it escapes,
        every expression in every page emits a CALLS to a method that does not
        exist."""
        _nodes, _edges, refs, _fi = _extract_one()
        assert not any(r.recv == _OUT_SENTINEL for r in refs)
        assert not any(r.target_name == "print" for r in refs)

    def test_el_property_becomes_a_typed_getter_call(self):
        """`${cart.total}` really does invoke cart.getTotal() at runtime, and
        useBean gives the declared type — so it can be bound precisely rather
        than fanning out across every getTotal in the repo."""
        _nodes, _edges, refs, _fi = _extract_one()
        el = [r for r in refs if r.target_name == "getTotal"]
        assert len(el) == 1
        assert el[0].recv == "cart"
        # SIMPLE name: java.py stores recv_type via simple_type_name and the
        # resolver's index is keyed that way. 'com.acme.Cart' would never match.
        assert el[0].recv_type == "Cart"
        assert el[0].ref_line == 16

    def test_el_on_unknown_receiver_is_not_bound(self):
        """Only declared beans have a known type. Binding `${foo.bar}` for an
        arbitrary request attribute would invent a call to a method whose
        owner is a guess."""
        body = '<html>${mystery.thing} ${cart.total}</html>\n'
        _nodes, _edges, refs, _fi = _extract_one("v/el.jsp", body)
        assert not any(r.target_name == "getThing" for r in refs)

    def test_el_chain_binds_only_the_first_hop(self):
        """`${order.customer.name}`: order's type is known, but getCustomer()'s
        return type is not, so `name` cannot be bound without guessing."""
        body = ('<jsp:useBean id="order" class="com.acme.Order"/>\n'
                '${order.customer.name}\n')
        _nodes, _edges, refs, _fi = _extract_one("v/chain.jsp", body)
        names = {r.target_name for r in refs if r.type == "CALLS"}
        assert "getCustomer" in names
        assert "getName" not in names

    def test_page_navigation_edges(self):
        """Include and forward are how one page reaches another — the top of
        every request path."""
        _nodes, _edges, refs, _fi = _extract_one()
        uses = {r.target_name for r in refs if r.type == "USES"}
        assert {"header.jspf", "done.jsp"} <= uses

    def test_declarations_become_fields(self):
        nodes, _edges, _refs, _fi = _extract_one()
        fields = {n.name for n in nodes if n.label == "Field"}
        assert "hitCount" in fields

    def test_all_nodes_marked_jsp(self):
        nodes, _edges, _refs, _fi = _extract_one()
        assert nodes and all(n.lang == "jsp" for n in nodes)


class TestRobustness:
    def test_pure_template_page(self):
        """No Java at all is normal for a JSP; it must still become a node."""
        nodes, _edges, _refs, _fi = _extract_one("v/plain.jsp", "<html><body>hi</body></html>\n")
        assert any(n.label == "File" for n in nodes)

    def test_unterminated_scriptlet_does_not_raise(self):
        nodes, _edges, _refs, _fi = _extract_one("v/broken.jsp", "<html><% int x = ;\n")
        assert any(n.label == "File" for n in nodes)

    def test_jsp_comments_ignored(self):
        body = "<%-- <% notCode(); %> --%>\n<html></html>\n"
        _nodes, _edges, refs, _fi = _extract_one("v/c.jsp", body)
        assert not any(r.target_name == "notCode" for r in refs)

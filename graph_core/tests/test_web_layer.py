"""The cross-language web layer: JSP <-> Java <-> JS.

WHY THESE ASSERTIONS AND NOT OTHERS
A JSP application's graph is three disconnected islands until something joins
them, and every join in this codebase is a STRING LITERAL rather than a symbol
reference. So the risks are not "does it parse" but:

  * the literal is only ever a fragment. `<script src>` is
    `<%=CONST%>foo.js<%=VER%>`, and a forward is `CONST + "foo.jsp"`. A test that
    feeds a clean absolute path proves nothing about the real input, so the
    fixtures below use the concatenated shapes the real pages use.
  * a loose literal matcher invents edges. `map.put("k", v)` looked exactly like
    an HTTP PUT until CALLS_API stopped being a dropped edge type — 24 of one
    servlet's 25 "HTTP calls" were map writes. That regression is pinned here.
  * a basename can match two files. Resolution must say AMBIGUOUS rather than
    pick, and must never cross languages (`common.js` vs `common.jsp`).
"""
from __future__ import annotations

import os
import tempfile

from graph_core.discovery import discover, list_candidate_relpaths
from graph_core.extractors import extract
from graph_core.extractors.java import (
    SERVLET_ANY_METHOD, _jsp_page_literal, _servlet_route,
)
from graph_core.extractors.javascript import _java_class_target, _servlet_url
from graph_core.resolver import resolve
from graph_core.schema import DROPPED_EDGE_TYPES, EDGE_TYPES

# A servlet in the shape the real codebase uses: @WebServlet for its own URL, a
# constant-prefixed forward for the page it renders, and the forward target held
# in a local rather than passed inline (that split is 44% of real call sites).
_SERVLET = """package scm.demo;
import jakarta.servlet.annotation.WebServlet;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

@WebServlet("/servlets/scm/demo/OrderServlet")
public class OrderServlet extends ValidateAccess {
    public void service(HttpServletRequest request, HttpServletResponse response) {
        java.util.Map params = new java.util.HashMap();
        params.put("sortSeqNo", "1");
        params.delete("/notAUrl");
        final String nextUrl = BaseConstant.BASE_JSP_DEMO_URL + "orderlist.jsp";
        getServletContext().getRequestDispatcher(nextUrl).forward(request, response);
    }
}
"""

_PAGE = """<%@ page import="com.acme.OrderService" %>
<HTML><HEAD>
<SCRIPT src="<%=BaseConstant.BASE_SCRIPT_DEMO_URL%>order-list.js<%=BROWSER_CACHE_VERSION%>"></SCRIPT>
</HEAD><BODY>
<% OrderService svc = new OrderService(); %>
</BODY></HTML>
"""

_SCRIPT = """function loadOrders() {
    var ajaxCall = new AjaxCallBackHandler('com.acme.demo.ajax.OrderAJAXHandler', cb, p);
    var url = WEB_APP_NAME + "/servlets/scm/demo/OrderServlet?orderId=" + id;
    ajaxCall.run(true, url);
}
function submitForm() {
    document.frm.action = BASE_SERVLET_DEMO_URL + "OrderServlet";
    document.frm.submit();
}
"""

_HANDLER = """package com.acme.demo.ajax;
public class OrderAJAXHandler {
    public String handle() { return "ok"; }
}
"""


def _corpus(tmp: str) -> tuple[list, list, list]:
    """Write the four files, extract and resolve them, return the graph."""
    layout = {
        "WEB-INF/classes/scm/demo/OrderServlet.java": _SERVLET,
        "WEB-INF/classes/com/acme/demo/ajax/OrderAJAXHandler.java": _HANDLER,
        "scm/demo/orderlist.jsp": _PAGE,
        "scm/demo/js/order-list.js": _SCRIPT,
    }
    for rel, body in layout.items():
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    nodes, edges, refs = [], [], []
    for info in discover(tmp, candidate_relpaths=list_candidate_relpaths(tmp)):
        n, e, r = extract(info, "t")
        nodes += n
        edges += e
        refs += r
    extra, out_edges, _cov = resolve(nodes, edges, refs, "t")
    # The graph is the UNION: EXPOSES/CONTAINS are emitted directly by the
    # extractor, everything name-based comes back from resolve. Checking only one
    # half would miss whichever side an edge is produced on.
    return nodes + extra, edges + out_edges, refs


def _live(out_edges):
    """Edges that actually reach Neo4j — store.write_edges drops the rest."""
    return [e for e in out_edges if e.type not in DROPPED_EDGE_TYPES]


def _pairs(nodes, out_edges, etype):
    by_id = {n.id: n for n in nodes}
    return {
        (getattr(by_id.get(e.src), "name", "?"), getattr(by_id.get(e.dst), "name", "?"))
        for e in _live(out_edges) if e.type == etype
    }


# --- the three links, end to end -------------------------------------------

def test_the_three_links_resolve():
    with tempfile.TemporaryDirectory() as tmp:
        nodes, out_edges, _ = _corpus(tmp)

        # Java -> JSP, via a literal held in a local, not in the forward call.
        assert ("service", "orderlist.jsp") in _pairs(nodes, out_edges, "RENDERS")
        # JSP -> JS, with the filename embedded between two <%= %> blocks.
        assert ("orderlist.jsp", "order-list.js") in _pairs(
            nodes, out_edges, "INCLUDES_SCRIPT")
        # JS -> Java, both spellings: the fully-qualified AJAX handler and the
        # bare servlet class named by a form action.
        handled = _pairs(nodes, out_edges, "HANDLED_BY")
        assert ("loadOrders", "OrderAJAXHandler") in handled
        assert ("submitForm", "OrderServlet") in handled
        # JS -> Java by URL, matched against the @WebServlet Endpoint.
        api = _pairs(nodes, out_edges, "CALLS_API")
        assert ("loadOrders", "ANY /servlets/scm/demo/OrderServlet") in api


def test_jsp_reaches_java_in_two_hops():
    """The point of the whole layer: a page connects to Java through its script.

    Asserted as a real traversal rather than as three separate edges, because
    three edges that never meet at a shared node are exactly the failure this is
    meant to rule out.
    """
    with tempfile.TemporaryDirectory() as tmp:
        nodes, out_edges, _ = _corpus(tmp)
        by_id = {n.id: n for n in nodes}
        live = _live(out_edges)

        page = next(n for n in nodes if n.name == "orderlist.jsp" and n.label == "File")
        scripts = [e.dst for e in live
                   if e.type == "INCLUDES_SCRIPT" and e.src == page.id]
        assert scripts, "page has no script edge"

        # hop 2: something declared inside that .js file reaches Java.
        js_files = {by_id[s].file for s in scripts if s in by_id}
        js_fn_ids = {n.id for n in nodes
                     if n.label == "Function" and n.file in js_files}
        reached = [
            by_id.get(e.dst) for e in live
            if e.src in js_fn_ids and e.type in ("HANDLED_BY", "CALLS_API")
        ]
        assert any(t is not None and t.lang == "java" for t in reached), (
            "no JSP -> JS -> Java path")


def test_webservlet_becomes_an_endpoint_on_the_handler_method():
    with tempfile.TemporaryDirectory() as tmp:
        nodes, out_edges, _ = _corpus(tmp)
        eps = [n for n in nodes if n.label == "Endpoint"]
        assert [e.route for e in eps] == ["/servlets/scm/demo/OrderServlet"]
        # Verb-agnostic on purpose: one servlet serves GET and POST alike, so a
        # concrete verb here would make a form POST miss the endpoint.
        assert eps[0].method == SERVLET_ANY_METHOD
        # EXPOSES hangs off service(), not off the class — a path has to continue
        # from the handler into the business logic.
        assert ("service", "ANY /servlets/scm/demo/OrderServlet") in _pairs(
            nodes, out_edges, "EXPOSES")


# --- the regression that motivated the receiver guard ----------------------

def test_map_put_is_not_an_http_call():
    """`map.put("sortSeqNo", v)` must not become `PUT /sortSeqNo`.

    This was live for as long as CALLS_API was dropped at write time: the edges
    were built and discarded, so nothing surfaced. Un-dropping CALLS_API turned it
    into one fabricated Endpoint per map key.
    """
    with tempfile.TemporaryDirectory() as tmp:
        nodes, out_edges, _ = _corpus(tmp)
        routes = {n.route for n in nodes if n.label == "Endpoint"}
        assert "/sortSeqNo" not in routes
        assert "/notAUrl" not in routes, "receiver guard missed a collection call"
        # The one real outbound call is still there.
        assert "/servlets/scm/demo/OrderServlet" in routes


# --- resolution honesty ----------------------------------------------------

def test_basename_never_crosses_language():
    """common.js and common.jsp share a basename and must not be confused."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, body in {
            "scm/demo/common.jsp": '<SCRIPT src="<%=P%>common.js"></SCRIPT>\n',
            "scm/demo/common.js": "function f() { return 1; }\n",
        }.items():
            path = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        nodes, edges, refs = [], [], []
        for info in discover(tmp, candidate_relpaths=list_candidate_relpaths(tmp)):
            n, e, r = extract(info, "t")
            nodes += n
            edges += e
            refs += r
        extra, out_edges, _cov = resolve(nodes, edges, refs, "t")
        by_id = {n.id: n for n in nodes + extra}
        for e in _live(out_edges):
            if e.type == "INCLUDES_SCRIPT":
                assert by_id[e.dst].lang == "javascript"


def test_ambiguous_page_name_is_not_guessed():
    """Two pages with one name: report ambiguity, never pick one."""
    with tempfile.TemporaryDirectory() as tmp:
        java = """package scm.demo;
public class R {
    public void go() {
        getServletContext().getRequestDispatcher(P + "index.jsp").forward(a, b);
    }
}
"""
        for rel, body in {
            "WEB-INF/classes/scm/demo/R.java": java,
            "a/index.jsp": "<% int x = 1; %>\n",
            "b/index.jsp": "<% int y = 2; %>\n",
        }.items():
            path = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        nodes, edges, refs = [], [], []
        for info in discover(tmp, candidate_relpaths=list_candidate_relpaths(tmp)):
            n, e, r = extract(info, "t")
            nodes += n
            edges += e
            refs += r
        _extra, out_edges, cov = resolve(nodes, edges, refs, "t")
        renders = [e for e in _live(out_edges) if e.type == "RENDERS"]
        assert len(renders) == 2, "both candidates should be emitted"
        assert {e.confidence for e in renders} == {"AMBIGUOUS"}
        assert cov["RENDERS"].ambiguous == 1


# --- unit level: the literal classifiers ----------------------------------

def test_servlet_url_extraction():
    assert _servlet_url('WEB/servlets/a/b/Foo?x=1') == "/servlets/a/b/Foo"
    assert _servlet_url("/servlets/a/b/Foo") == "/servlets/a/b/Foo"
    assert _servlet_url("no servlet here") == ""
    assert _servlet_url("/servlets/") == ""          # nothing after the prefix


def test_java_class_target_rejects_non_classes():
    assert _java_class_target("com.acme.demo.ajax.OrderAJAXHandler")
    assert _java_class_target("OrderServlet") == "OrderServlet"
    # A dotted config key is not a class: the tail is lower-case.
    assert _java_class_target("com.acme.some.property") == ""
    # A bare capitalised word is just a word without the Servlet suffix.
    assert _java_class_target("DUMMY") == ""
    assert _java_class_target("Order") == ""
    assert _java_class_target("hello world") == ""
    assert _java_class_target("/servlets/a/b") == ""


def test_jsp_page_literal_rejects_prose():
    class _N:
        """Minimal stand-in: _jsp_page_literal only needs the literal's text."""
        type = "string_literal"

        def __init__(self, s):
            self.text = f'"{s}"'.encode("utf-8")
            self.start_byte, self.end_byte = 0, len(self.text)

    def page(s):
        return _jsp_page_literal(_N(s).text, _N(s))

    assert page("orderlist.jsp") == "orderlist.jsp"
    assert page("/scm/demo/orderlist.jsp") == "orderlist.jsp"
    assert page("orderlist.jsp?mode=Q") == "orderlist.jsp"
    assert page("forwarding to the .jsp now") == ""
    assert page("<%=x%>.jsp") == ""
    assert page("order.html") == ""


def test_new_edge_types_are_registered_and_not_dropped():
    """A type missing from EDGE_TYPES crashes at write; one left in
    DROPPED_EDGE_TYPES is silently discarded. Both are invisible until a run."""
    for t in ("RENDERS", "INCLUDES_SCRIPT", "HANDLED_BY", "CALLS_API", "INCLUDES_PAGE"):
        assert t in EDGE_TYPES, f"{t} would fail assert_edge at write time"
        assert t not in DROPPED_EDGE_TYPES, f"{t} would be silently dropped"

"""Stage 1 — Java structural extraction via tree-sitter.

Emits:
    Nodes: File, Class(kind=class|interface|enum|record), Function(kind=method|
           constructor), Field — with metadata (range incl. columns, visibility,
           modifiers, is_static/abstract, return_type, param_count).
    Edges (resolved): CONTAINS — with provenance.
        RawRefs (name-only): IMPORTS, EXTENDS, IMPLEMENTS, CALLS, INSTANTIATES,
            ANNOTATED_WITH — each carrying the reference-site location.
"""
from __future__ import annotations

import os

from ..apispec import (
    HTTP_METHODS,
    endpoint_display,
    endpoint_fqn,
    endpoint_id,
    normalize_route,
    split_url,
)
from ..discovery import FileInfo
from ..ids import body_hash, make_id
from ..models import Edge, Node, Origin, RawRef
from ..languages import get_parser
from .common import iter_descendants, simple_type_name, text

EXTRACTOR = "tree-sitter"

# Spring mapping annotations -> HTTP method.
_SPRING_MAPPING = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
# The servlet's own URL, declared on the class: @WebServlet("/servlets/a/b/Foo").
# This is the ENTRY POINT of a classic Java web app, and until it was read here
# such an app produced almost no Endpoint nodes at all — _SPRING_MAPPING only
# knows Spring MVC, so a JAX-WS/servlet codebase looked like it had no HTTP
# surface, and every reachability pass that seeds from EXPOSES started empty.
_SERVLET_ANNOTATION = "WebServlet"

# @WebServlet carries no HTTP verb — one servlet serves every method. Endpoints
# are keyed (METHOD, route), so they need SOME verb, and picking GET would make
# a form POST to the same URL miss. This sentinel is matched specially by the
# resolver (see match_endpoints), which falls back to it when the caller's own
# verb finds nothing.
SERVLET_ANY_METHOD = "ANY"

# The methods a servlet container actually dispatches to. Measured on a 16.7k
# -file JSP app: service() in 3,330 files, doGet/doPost/doDelete in 20 between
# them — so `service` is the handler in practice, but the doXxx family is listed
# because when a servlet does use them, they are the only handler it has.
_SERVLET_ENTRY_METHODS = {
    "service", "doGet", "doPost", "doPut", "doDelete", "doHead", "doOptions",
}

# RestTemplate method -> HTTP verb (outbound call detection).
_REST_TEMPLATE_CALLS = {
    "getForObject": "GET", "getForEntity": "GET",
    "postForObject": "POST", "postForEntity": "POST", "postForLocation": "POST",
    "put": "PUT", "delete": "DELETE",
}
# The two entries above that are also everyday Map/Collection methods. These
# require receiver evidence before they count as an HTTP call — see
# _outbound_java for what happens without it.
_AMBIGUOUS_REST_CALLS = frozenset({"put", "delete"})
# Substrings of a receiver name that mean "this is an HTTP client". Matched as a
# substring so `restTemplate`, `myRestTemplate` and `sftRestTemplate` all pass.
# The Java analogue of apispec.HTTP_CLIENT_RECEIVERS.
_JAVA_HTTP_RECEIVER_HINTS = ("resttemplate", "webclient", "httpclient", "feign")

_AUTH_REQUIRE_ANNOTATIONS = {
    "Authenticated", "AuthenticationPrincipal", "LoginRequired",
    "RequiresAuthentication",
}
# Spring/Jakarta DI annotations that mark a field/constructor param as an
# injected dependency -> emitted as an AUTOWIRED type-shaped ref (schema.py
# already reserves this edge type; nothing emitted it until now). Ported from
# primitive-pr's Java resolution work (same annotation set, same single-
# constructor inference rule) so graph_rag gets equivalent DI-edge coverage.
_JAVA_AUTOWIRE_ANNOTATIONS = {"Autowired", "Inject", "Resource"}
_POLICY_ANNOTATIONS = {
    "PreAuthorize", "Secured", "RolesAllowed", "PermissionsAllowed",
    "RequiresRoles", "RequiresPermissions",
}
_EVENT_CONSUMER_ANNOTATIONS = {
    "KafkaListener", "RabbitListener", "JmsListener", "SqsListener",
}
_EVENT_EMIT_METHODS_STRONG = {
    "publish", "emit", "produce", "publishEvent", "dispatch",
}
_EVENT_EMIT_METHODS_GENERIC = {"send"}
_EVENT_CONSUME_METHODS_STRONG = {"subscribe", "consume", "registerListener"}
_EVENT_CONSUME_METHODS_GENERIC = {"listen", "on"}
_JAVA_EVENT_RECEIVER_HINTS = {
    "bus", "broker", "producer", "consumer", "emitter", "events",
    "eventBus", "kafka", "queue", "topic", "pubsub", "publisher", "channel",
}

_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}

_VISIBILITY = {"public", "private", "protected"}
_MODIFIER_KEYWORDS = {
    "static", "final", "abstract", "synchronized", "native", "transient",
    "volatile", "default", "sealed", "strictfp",
}


def extract(file: FileInfo, repo: str):
    src = file.source
    tree = get_parser("java").parse(src)
    root = tree.root_node

    nodes: list[Node] = []
    edges: list[Edge] = []
    refs: list[RawRef] = []

    def contains(container_id: str, child_node, child_id: str):
        edges.append(Edge(
            "CONTAINS", container_id, child_id,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=child_node.start_point[0] + 1,
            evidence_col=child_node.start_point[1],
        ))

    def ref(rtype, src_id, target, kind_hint, node, recv="", call_arity=-1,
            strategy_hint="", recv_type=""):
        refs.append(RawRef(
            rtype, src_id, target, kind_hint, recv=recv,
            ref_file=file.relpath,
            ref_line=node.start_point[0] + 1, ref_col=node.start_point[1],
            call_arity=call_arity,
            strategy_hint=strategy_hint,
            recv_type=recv_type,
        ))

    def emit_endpoint(method, route, handler_id, ev_node):
        eid = endpoint_id(repo, method, route)
        nodes.append(Node(
            id=eid, label="Endpoint", name=endpoint_display(method, route),
            fqn=endpoint_fqn(method, route), repo=repo, kind="endpoint",
            lang="java", file=file.relpath,
            start_line=ev_node.start_point[0] + 1, start_col=ev_node.start_point[1],
            end_line=ev_node.end_point[0] + 1, end_col=ev_node.end_point[1],
            method=method.upper(), route=normalize_route(route), extractor=EXTRACTOR,
        ))
        edges.append(Edge(
            "EXPOSES", handler_id, eid,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=ev_node.start_point[0] + 1, evidence_col=ev_node.start_point[1],
        ))

    def emit_api_call(handler_id, method, url, ev_node):
        host, path = split_url(url)
        refs.append(RawRef(
            "CALLS_API", handler_id, path, "api", recv=host, http_method=method,
            ref_file=file.relpath,
            ref_line=ev_node.start_point[0] + 1, ref_col=ev_node.start_point[1],
        ))

    file_fqn = file.relpath
    file_id = make_id(repo, file_fqn, "file")

    package = ""
    for child in root.children:
        if child.type == "package_declaration":
            for c in child.children:
                if c.type in ("scoped_identifier", "identifier"):
                    package = text(src, c)
        elif child.type == "import_declaration":
            name, fqn, is_wildcard = _import_name(src, child)
            if name:
                if is_wildcard:
                    # `import com.foo.util.*;` -- target_name marks the wildcard
                    # explicitly (trailing ".*") so the resolver can expand it
                    # to every in-repo class under that package, instead of
                    # being mistaken for an import of a class literally named
                    # after the package's last segment.
                    ref("IMPORTS", file_id, f"{name}.*", "import_wildcard", child)
                    refs[-1].import_fqn = name
                else:
                    ref("IMPORTS", file_id, name, "import", child)
                    refs[-1].import_fqn = fqn

    nodes.append(Node(
        id=file_id, label="File", name=os.path.basename(file.relpath),
        fqn=file_fqn, repo=repo, kind="file", lang="java", file=file.relpath,
        package=package, start_line=1, start_col=0, end_line=root.end_point[0] + 1,
        end_col=root.end_point[1], body_hash=file.sha, extractor=EXTRACTOR,
    ))

    def modifiers_of(node):
        """Return (visibility, [modifier keywords], [annotation names])."""
        vis, mods, anns = "", [], []
        for child in node.children:
            if child.type == "modifiers":
                for m in child.children:
                    if m.type in _VISIBILITY:
                        vis = m.type
                    elif m.type in _MODIFIER_KEYWORDS:
                        mods.append(m.type)
                    elif m.type in ("annotation", "marker_annotation"):
                        nm = m.child_by_field_name("name")
                        if nm:
                            anns.append(simple_type_name(text(src, nm)))
        return (vis or "package"), mods, anns

    def walk_type(node, parent_fqn, container_id):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(src, name_node)
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        kind = _TYPE_DECLS[node.type]
        vis, mods, anns = modifiers_of(node)
        cid = make_id(repo, fqn, kind)
        nodes.append(Node(
            id=cid, label="Class", name=name, fqn=fqn, repo=repo, kind=kind,
            lang="java", file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=vis, modifiers=mods, is_abstract="abstract" in mods,
            is_static="static" in mods, body_hash=body_hash(text(src, node)),
            extractor=EXTRACTOR,
        ))
        contains(container_id, node, cid)
        for ann in anns:
            ref("ANNOTATED_WITH", cid, ann, "annotation", node)
        for et in _java_auth_policy_specs(src, node):
            ref(et[0], cid, et[1], "policy", node, strategy_hint="fuzzy_name" if et[2] else "")

        sc = node.child_by_field_name("superclass")
        if sc:
            for t in _types_in(src, sc):
                ref("EXTENDS", cid, t, "type", sc)
        si = node.child_by_field_name("interfaces")
        if si:
            for t in _types_in(src, si):
                ref("IMPLEMENTS", cid, t, "type", si)

        route_prefix = _request_mapping_prefix(src, node)
        # The servlet's declared URL, if any. Held on the CLASS but exposed by
        # its handler METHODS, because EXPOSES is Function -> Endpoint and it is
        # the handler a caller actually reaches.
        servlet_route = _servlet_route(src, node)
        body = node.child_by_field_name("body")
        if body:
            members = body.children
            constructors = [c for c in members if c.type == "constructor_declaration"]
            # Field name -> declared type, collected BEFORE any method is walked:
            # a method may call through a field declared further down the class,
            # and Java has no forward-declaration rule to stop it. This is the
            # outermost scope of the receiver-type map each method builds on top
            # of (see walk_method) — without it every `this.someField.doThing()`
            # and bare `someField.doThing()` loses its receiver and falls through
            # to the resolver's global name match.
            field_types: dict[str, str] = {}
            for child in members:
                if child.type != "field_declaration":
                    continue
                _t = child.child_by_field_name("type")
                _ft = simple_type_name(text(src, _t)) if _t is not None else ""
                if not _ft:
                    continue
                for _d in child.children:
                    if _d.type == "variable_declarator":
                        _nm = _d.child_by_field_name("name")
                        if _nm:
                            field_types[text(src, _nm)] = _ft
            for child in members:
                if child.type in ("method_declaration", "constructor_declaration"):
                    walk_method(child, fqn, cid, route_prefix, field_types,
                                servlet_route)
                    if child.type == "constructor_declaration":
                        _extract_ctor_di(child, cid, single=len(constructors) == 1)
                elif child.type == "field_declaration":
                    walk_field(child, fqn, cid)
                elif child.type in _TYPE_DECLS:
                    walk_type(child, fqn, cid)

    def _extract_ctor_di(ctor_node, class_id, single: bool):
        """DI edge for constructor-injected dependencies: emitted when the
        constructor is itself @Autowired/@Inject, OR it's the class's only
        constructor (Spring's implicit-injection convention since 4.3 — no
        annotation needed on a single constructor)."""
        _, _, ctor_anns = modifiers_of(ctor_node)
        if not single and not set(ctor_anns) & _JAVA_AUTOWIRE_ANNOTATIONS:
            return
        params_node = ctor_node.child_by_field_name("parameters")
        if params_node is None:
            return
        for p in params_node.children:
            if p.type not in ("formal_parameter", "spread_parameter"):
                continue
            t = p.child_by_field_name("type")
            if t is not None:
                ref("AUTOWIRED", class_id, simple_type_name(text(src, t)), "type", p)

    def walk_method(node, class_fqn, class_id, route_prefix="", field_types=None,
                    servlet_route=""):
        name_node = node.child_by_field_name("name")
        is_ctor = node.type == "constructor_declaration"
        name = text(src, name_node) if name_node else (class_fqn.rsplit(".", 1)[-1] if is_ctor else "<anon>")
        params_node = node.child_by_field_name("parameters")
        params = text(src, params_node) if params_node else "()"
        signature = f"{name}{params}"
        vis, mods, anns = modifiers_of(node)
        rt_node = node.child_by_field_name("type")
        return_type = simple_type_name(text(src, rt_node)) if rt_node is not None else ""
        body = node.child_by_field_name("body")
        branch_count, loop_count = _complexity_counts(body)
        cyclomatic = 1 + branch_count + loop_count
        _pnames, _ptypes = _params(src, params_node)
        fqn = f"{class_fqn}#{name}"
        mid = make_id(repo, f"{fqn}{params}", "method")
        nodes.append(Node(
            id=mid, label="Function", name=name, fqn=fqn, repo=repo,
            kind="constructor" if is_ctor else "method", lang="java",
            file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=vis, modifiers=mods, is_static="static" in mods,
            is_abstract="abstract" in mods, return_type=return_type,
            param_count=_param_count(params_node),
            param_names=_pnames, param_types=_ptypes, signature=signature,
            loc=(node.end_point[0] - node.start_point[0]) + 1,
            cyclomatic=cyclomatic,
            branch_count=branch_count,
            loop_count=loop_count,
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(class_id, node, mid)
        for ann in anns:
            ref("ANNOTATED_WITH", mid, ann, "annotation", node)
        for et in _java_auth_policy_specs(src, node):
            ref(et[0], mid, et[1], "policy", node, strategy_hint="fuzzy_name" if et[2] else "")
        for topic in _java_event_consumer_topics(src, node):
            ref("CONSUMES_EVENT", mid, topic, "event", node)
        for method, route in _spring_endpoints(src, node, route_prefix):
            emit_endpoint(method, route, mid, node)
        # The class's @WebServlet URL, exposed by each container-dispatched
        # handler. Emitted per handler rather than once per class so the graph
        # says which METHOD serves the route, which is what a path needs to
        # continue into the business logic.
        if servlet_route and name in _SERVLET_ENTRY_METHODS and not is_ctor:
            emit_endpoint(SERVLET_ANY_METHOD, servlet_route, mid, node)
        # type edges: return type + parameter types
        if rt_node is not None and not is_ctor:
            _emit_type(ref, "RETURNS", mid, src, rt_node)
        if params_node is not None:
            for p in params_node.children:
                if p.type in ("formal_parameter", "spread_parameter"):
                    t = p.child_by_field_name("type")
                    if t is not None:
                        _emit_type(ref, "HAS_TYPE", mid, src, t)

        # checked exceptions declared in the `throws` clause
        for c in node.children:
            if c.type == "throws":
                for t in _types_in(src, c):
                    ref("THROWS", mid, t, "type", c)

        # Receiver-type map for this method body, innermost scope last so the
        # normal Java shadowing order (local > parameter > field) falls out of
        # plain dict overwrite. Block-level scoping is deliberately flattened:
        # re-declaring the same name at a different type within one method is
        # rare, and a flat map is still enormously better than the nothing that
        # was here before — every Java call used to emit an empty receiver.
        var_types: dict[str, str] = dict(field_types or {})
        for _nm, _tp in zip(_pnames, _ptypes):
            if _nm and _tp:
                var_types[_nm] = _tp[:-3] if _tp.endswith("...") else _tp
        if body:
            for _d in iter_descendants(body):
                if _d.type != "local_variable_declaration":
                    continue
                _t = _d.child_by_field_name("type")
                _tp = simple_type_name(text(src, _t)) if _t is not None else ""
                if not _tp:
                    continue
                for _vd in _d.children:
                    if _vd.type != "variable_declarator":
                        continue
                    _nm = _vd.child_by_field_name("name")
                    if not _nm:
                        continue
                    _resolved = _tp
                    if _tp == "var":
                        # Java 10+ `var x = new Foo()` — the declared type node
                        # says "var", so take the type off the initializer when
                        # it is a constructor call (the only form where it is
                        # syntactically available without real inference).
                        _val = _vd.child_by_field_name("value")
                        if _val is not None and _val.type == "object_creation_expression":
                            _vt = _val.child_by_field_name("type")
                            _resolved = simple_type_name(text(src, _vt)) if _vt is not None else ""
                        else:
                            _resolved = ""
                    if _resolved:
                        var_types[text(src, _nm)] = _resolved

        def _receiver(inv):
            """(recv, recv_type) for a method_invocation.

            recv_type is what actually matters — it turns on narrow_call's
            receiver-type step, which narrows to that class's methods instead
            of every same-named method in the repo. recv is still passed so the
            resolver's receiver-is-a-class-name step can catch static calls
            (`PathUtil.normalize()`), where the identifier IS the type.
            """
            obj = inv.child_by_field_name("object")
            cls_simple = class_fqn.rsplit(".", 1)[-1]
            if obj is None:                      # unqualified call -> implicit this
                return "this", cls_simple
            if obj.type == "this":
                return "this", cls_simple
            if obj.type == "identifier":
                nm = text(src, obj)
                # Unknown identifier: most likely a class name (static call).
                # Leave recv_type empty and let `recv` drive the class-name step
                # rather than inventing a type that would narrow to nothing.
                return nm, var_types.get(nm, "")
            if obj.type == "field_access":
                fname = _this_field(src, obj)    # `this.foo.bar()`
                if fname:
                    return fname, var_types.get(fname, "")
            # Chained/complex receivers (`a.b().c()`, casts, array access) need
            # return-type propagation to resolve — out of scope here, so they
            # keep the previous behaviour of no receiver information at all.
            return "", ""

        if body:
            # field-writes: `this.x = ...` (LHS of an assignment)
            write_fa = set()
            for d in iter_descendants(body):
                if d.type == "assignment_expression":
                    left = d.child_by_field_name("left")
                    if left is not None and _this_field(src, left):
                        write_fa.add(left.id)   # stable tree-sitter node id
            for d in iter_descendants(body):
                if d.type == "method_invocation":
                    nm = d.child_by_field_name("name")
                    if nm:
                        _recv, _recv_type = _receiver(d)
                        ref(
                            "CALLS",
                            mid,
                            text(src, nm),
                            "call",
                            d,
                            recv=_recv,
                            recv_type=_recv_type,
                            call_arity=_call_arity(d),
                        )
                        ob = _outbound_java(src, d, text(src, nm))
                        if ob is not None:
                            emit_api_call(mid, ob[0], ob[1], d)
                        ev_result = _outbound_event_java(src, d, text(src, nm))
                        if ev_result[0]:
                            ref("EMITS_EVENT", mid, ev_result[0], "event", d,
                                strategy_hint="fuzzy_name" if ev_result[1] else "")
                        evc_result = _inbound_event_java(src, d, text(src, nm))
                        if evc_result[0]:
                            ref("CONSUMES_EVENT", mid, evc_result[0], "event", d,
                                strategy_hint="fuzzy_name" if evc_result[1] else "")
                elif d.type == "object_creation_expression":
                    tp = d.child_by_field_name("type")
                    if tp:
                        ref("INSTANTIATES", mid, simple_type_name(text(src, tp)), "type", d)
                elif d.type == "throw_statement":
                    for t in _thrown_types(src, d):
                        ref("THROWS", mid, t, "type", d)
                elif d.type == "catch_formal_parameter":
                    for t in _types_in(src, d):
                        ref("CATCHES", mid, t, "type", d)
                elif d.type == "field_access":
                    fname = _this_field(src, d)
                    if fname:
                        ref("WRITES" if d.id in write_fa else "READS",
                            mid, fname, "field", d, recv="this")
                elif d.type == "string_literal":
                    # Which JSP this method renders. Scoped to the whole METHOD
                    # BODY rather than to the forward call's argument, and that is
                    # the entire reason it works: measured on a 16.7k-file JSP
                    # app, only 1,827 of 3,280 getRequestDispatcher calls hold the
                    # literal themselves — the rest read a local assigned a few
                    # lines earlier (`final String nextUrl = PREFIX + "x.jsp";`).
                    # Matching the argument alone would miss 44% of forwards.
                    #
                    # The path is almost never wholly literal either: the real
                    # shape is `PREFIX_CONSTANT + "adhocquery.jsp"`, so only the
                    # basename is available and only the basename is used. The
                    # resolver matches it against File nodes, and a name matching
                    # two pages resolves AMBIGUOUS rather than picking one.
                    page = _jsp_page_literal(src, d)
                    if page:
                        ref("RENDERS", mid, page, "file", d)

    def walk_field(node, class_fqn, class_id):
        vis, mods, anns = modifiers_of(node)
        type_node = node.child_by_field_name("type")
        ftype = simple_type_name(text(src, type_node)) if type_node is not None else ""
        if ftype and set(anns) & _JAVA_AUTOWIRE_ANNOTATIONS:
            ref("AUTOWIRED", class_id, ftype, "type", node)
        for d in node.children:
            if d.type == "variable_declarator":
                nm = d.child_by_field_name("name")
                if nm:
                    fname = text(src, nm)
                    ffqn = f"{class_fqn}.{fname}"
                    fid = make_id(repo, ffqn, "field")
                    nodes.append(Node(
                        id=fid, label="Field", name=fname, fqn=ffqn, repo=repo,
                        kind="field", lang="java", file=file.relpath,
                        start_line=node.start_point[0] + 1, start_col=node.start_point[1],
                        end_line=node.end_point[0] + 1, end_col=node.end_point[1],
                        visibility=vis, modifiers=mods, is_static="static" in mods,
                        return_type=ftype, extractor=EXTRACTOR,
                    ))
                    contains(class_id, node, fid)
                    if type_node is not None:
                        _emit_type(ref, "OF_TYPE", fid, src, type_node)

    for child in root.children:
        if child.type in _TYPE_DECLS:
            walk_type(child, package, file_id)

    return nodes, edges, refs


_PRIMITIVES = {
    "void", "int", "long", "double", "float", "boolean", "char", "byte", "short",
}


def _emit_type(ref, primary: str, src_id: str, src: bytes, type_node) -> None:
    """Emit `primary` to the base type and HAS_GENERIC to each generic arg
    (skipping primitives, which can never be an in-repo Class)."""
    base, args = _java_type_parts(src, type_node)
    if base and base not in _PRIMITIVES:
        ref(primary, src_id, base, "type", type_node)
    for g in args:
        if g not in _PRIMITIVES:
            ref("HAS_GENERIC", src_id, g, "type", type_node)


def _java_type_parts(src: bytes, node):
    """Return (base simple-name, [generic-arg simple-names]) for a type node."""
    base = simple_type_name(text(src, node))
    args = []
    for d in iter_descendants(node):
        if d.type == "type_arguments":
            for a in d.children:
                if a.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                    nm = simple_type_name(text(src, a))
                    if nm and nm not in args:
                        args.append(nm)
    return base, args


def _this_field(src: bytes, fa) -> str:
    """If `fa` is `this.<x>`, return 'x'; else ''."""
    if fa.type != "field_access":
        return ""
    obj = fa.child_by_field_name("object")
    if obj is None or obj.type != "this":
        return ""
    f = fa.child_by_field_name("field")
    return text(src, f) if f else ""


def _thrown_types(src: bytes, throw_node):
    """Exception type names from `throw new X(...)` (rethrown vars are skipped)."""
    out = []
    for d in [throw_node, *iter_descendants(throw_node)]:
        if d.type == "object_creation_expression":
            tp = d.child_by_field_name("type")
            if tp is not None:
                nm = simple_type_name(text(src, tp))
                if nm:
                    out.append(nm)
    return out


def _param_count(params_node) -> int:
    if params_node is None:
        return 0
    return sum(1 for c in params_node.children
               if c.type in ("formal_parameter", "spread_parameter"))


def _params(src: bytes, params_node):
    """Ordered (names, types) for a method's formal parameters. Varargs keep
    a trailing `...` on the type."""
    names: list[str] = []
    types: list[str] = []
    if params_node is None:
        return names, types
    for c in params_node.children:
        if c.type not in ("formal_parameter", "spread_parameter"):
            continue
        t = c.child_by_field_name("type")
        tp = simple_type_name(text(src, t)) if t is not None else ""
        n_ = c.child_by_field_name("name")
        if n_ is None:  # spread_parameter holds its name in a variable_declarator
            vd = next((cc for cc in c.children if cc.type == "variable_declarator"), None)
            n_ = vd.child_by_field_name("name") if vd is not None else None
        nm = text(src, n_) if n_ is not None else ""
        if c.type == "spread_parameter" and tp:
            tp += "..."
        names.append(nm)
        types.append(tp)
    return names, types


def _import_name(src: bytes, node) -> tuple[str, str, bool]:
    # Returns (target_name, fully_qualified_path, is_wildcard).
    # `import a.b.C;`  -> ("C", "a.b.C", False) -- target_name is the simple
    #     class name (what call/type sites reference), fqn is the full path
    #     (what the resolver uses to disambiguate same-named classes in
    #     different packages).
    # `import a.b.*;`  -> ("a.b", "a.b", True) -- the wildcard's package prefix.
    # `import static a.B.x;` -> treated like a normal import of `a.B` (the
    #     static member itself isn't a class we model).
    is_wildcard = any(c.type == "asterisk" for c in node.children)
    scoped = None
    for c in node.children:
        if c.type in ("scoped_identifier", "identifier"):
            scoped = c
    if scoped is None:
        return "", "", False
    full = text(src, scoped)
    if is_wildcard:
        return full, full, True
    tail = full.rsplit(".", 1)[-1]
    return tail, full, False


def _call_arity(call_node) -> int:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return -1
    count = 0
    for c in args.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count



def _str_lit(src: bytes, node) -> str:
    """Literal value of a Java string_literal, without quotes."""
    if node.type != "string_literal":
        return ""
    raw = text(src, node)
    return raw[1:-1] if len(raw) >= 2 and raw[0] == '"' else raw


def _annotation_nodes(node):
    for child in node.children:
        if child.type == "modifiers":
            for m in child.children:
                if m.type in ("annotation", "marker_annotation"):
                    yield m


def _annotation_value(src: bytes, ann_node) -> str:
    """The route string of a mapping annotation: a bare value or value=/path=."""
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "string_literal":
            return _str_lit(src, c)
        if c.type == "element_value_pair":
            key = c.child_by_field_name("key")
            val = c.child_by_field_name("value")
            if key is not None and text(src, key) in ("value", "path") \
                    and val is not None and val.type == "string_literal":
                return _str_lit(src, val)
    return ""


def _request_mapping_method(src: bytes, ann_node) -> str:
    """@RequestMapping(method = RequestMethod.POST) -> 'POST' (default '')."""
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "element_value_pair":
            key = c.child_by_field_name("key")
            val = c.child_by_field_name("value")
            if key is not None and text(src, key) == "method" and val is not None:
                return text(src, val).rsplit(".", 1)[-1].upper()
    return ""


def _servlet_route(src: bytes, type_node) -> str:
    """The URL from a class-level ``@WebServlet``, or '' if there is none.

    Reuses ``_annotation_value``, which already reads a bare string argument as
    well as ``value=``/``path=`` — the three spellings @WebServlet accepts
    (``urlPatterns=`` is handled by the ``value``/``path`` branch failing over to
    the bare-literal branch when only one pattern is given).

    Multi-pattern ``urlPatterns = {"/a", "/b"}`` yields only the first literal.
    Deliberate: a second Endpoint for the same handler adds a node and an edge
    that no query distinguishes, and the first pattern is the canonical one in
    every case measured.
    """
    for ann in _annotation_nodes(type_node):
        nm = ann.child_by_field_name("name")
        if nm is not None and simple_type_name(text(src, nm)) == _SERVLET_ANNOTATION:
            return _annotation_value(src, ann)
    return ""


def _jsp_page_literal(src: bytes, lit_node) -> str:
    """The bare page name if this string literal names a JSP, else ''.

    Returns the BASENAME only. The literal is typically the tail of a
    concatenation whose head is a constant (``BASE_JSP_ADHOC_URL + "x.jsp"``), so
    a full path is not available; matching on basename is what makes the
    resolution possible at all, and the resolver keeps it honest by reporting
    AMBIGUOUS when two pages share a name.

    Query strings are stripped (``"x.jsp?mode=Q"``). Anything that is not a
    single path-like token is rejected, so a literal that merely mentions ``.jsp``
    inside a sentence or a regex does not become an edge.
    """
    raw = _str_lit(src, lit_node).strip()
    if not raw or ".jsp" not in raw.lower():
        return ""
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith((".jsp", ".jspf")):
        return ""
    # A real page name is one path token: letters, digits, _ - . only. This is
    # what rejects log messages and patterns that happen to contain ".jsp".
    stem = name.rsplit(".", 1)[0]
    if not stem or any(ch in name for ch in " \t\"'<>%$*(){}[]+,;:=&"):
        return ""
    return name


def _request_mapping_prefix(src: bytes, type_node) -> str:
    """Class-level @RequestMapping route prefix, or '' if none."""
    for ann in _annotation_nodes(type_node):
        nm = ann.child_by_field_name("name")
        if nm is not None and simple_type_name(text(src, nm)) == "RequestMapping":
            return _annotation_value(src, ann)
    return ""


def _spring_endpoints(src: bytes, method_node, prefix: str):
    """(METHOD, route) for each Spring mapping annotation on a method."""
    out = []
    for ann in _annotation_nodes(method_node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        if name in _SPRING_MAPPING:
            out.append((_SPRING_MAPPING[name], prefix + _annotation_value(src, ann)))
        elif name == "RequestMapping":
            verb = _request_mapping_method(src, ann) or "GET"
            out.append((verb, prefix + _annotation_value(src, ann)))
    return out


def _outbound_java(src: bytes, call_node, name: str):
    """If this invocation is a RestTemplate-style HTTP call, return (METHOD, url).

    TWO GUARDS, AND THEY ARE NOT OPTIONAL. `_REST_TEMPLATE_CALLS` contains `put`
    and `delete`, which are also the two most common Map/Collection methods in
    Java. With the method name as the only test, every `map.put("sortSeqNo", v)`
    read as an HTTP PUT to `/sortSeqNo` — measured on one adhoc servlet, 24 of its
    25 detected "HTTP calls" were map writes.

    That produced no visible damage for as long as CALLS_API sat in
    DROPPED_EDGE_TYPES, because the edges were built and then discarded at write
    time. The moment CALLS_API became a real edge (it now carries the JS -> servlet
    hop) the same code would have minted one bogus Endpoint node per distinct map
    key in the repo. A dropped edge type hides the cost of a loose detector.
    """
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.children if c.type not in ("(", ")", ",")), None)
    if first is None or first.type != "string_literal":
        return None
    url = _str_lit(src, first)
    # Guard 1: it has to look like a URL. A map key does not.
    if not url or not (url.startswith("/") or "://" in url):
        return None
    if name in _REST_TEMPLATE_CALLS:
        # Guard 2: for the verbs that collide with ordinary collection methods,
        # the receiver must actually be an HTTP client. The distinctive names
        # (getForObject/postForEntity/...) need no such check — nothing else is
        # called that.
        if name in _AMBIGUOUS_REST_CALLS:
            recv = _java_recv_tail(call_node).lower()
            if not any(hint in recv for hint in _JAVA_HTTP_RECEIVER_HINTS):
                return None
        return _REST_TEMPLATE_CALLS[name], url
    if name == "exchange":
        # exchange(url, HttpMethod.POST, ...) -> verb from the 2nd argument
        pos = [c for c in args.children if c.type not in ("(", ")", ",")]
        verb = "GET"
        if len(pos) >= 2:
            verb = text(src, pos[1]).rsplit(".", 1)[-1].upper()
        return verb, url
    return None


def _java_recv_tail(call_node) -> str:
    """Tail name of the method invocation's object (receiver)."""
    obj = call_node.child_by_field_name("object")
    if obj is None:
        return ""
    raw = obj.text.decode("utf-8", "replace") if obj.text else ""
    return raw.split(".")[-1] if raw else ""


def _outbound_event_java(src: bytes, call_node, name: str) -> tuple[str, bool]:
    if name in _EVENT_EMIT_METHODS_STRONG:
        return _first_string_arg(src, call_node, 0), False
    if name in _EVENT_EMIT_METHODS_GENERIC:
        recv = _java_recv_tail(call_node)
        topic = _first_string_arg(src, call_node, 0)
        if not topic:
            return "", False
        return topic, recv not in _JAVA_EVENT_RECEIVER_HINTS
    return "", False


def _inbound_event_java(src: bytes, call_node, name: str) -> tuple[str, bool]:
    if name in _EVENT_CONSUME_METHODS_STRONG:
        return _first_string_arg(src, call_node, 0), False
    if name in _EVENT_CONSUME_METHODS_GENERIC:
        recv = _java_recv_tail(call_node)
        topic = _first_string_arg(src, call_node, 0)
        if not topic:
            return "", False
        return topic, recv not in _JAVA_EVENT_RECEIVER_HINTS
    return "", False


def _first_string_arg(src: bytes, call_node, index: int) -> str:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return ""
    pos = [c for c in args.children if c.type not in ("(", ")", ",")]
    if index >= len(pos) or pos[index].type != "string_literal":
        return ""
    return _str_lit(src, pos[index]).strip()


def _java_auth_policy_specs(src: bytes, node) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for ann in _annotation_nodes(node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        low = name.lower()
        if name in _AUTH_REQUIRE_ANNOTATIONS:
            out.append(("REQUIRES_AUTH", "AUTH_REQUIRED", False))
        elif "auth" in low:
            out.append(("REQUIRES_AUTH", "AUTH_REQUIRED", True))
        if name in _POLICY_ANNOTATIONS:
            target = _annotation_policy_value(src, ann) or name or "POLICY"
            out.append(("ENFORCES_POLICY", target, False))
        elif any(t in low for t in ("role", "permission", "policy", "scope", "authorize", "secured")):
            target = _annotation_policy_value(src, ann) or name or "POLICY"
            out.append(("ENFORCES_POLICY", target, True))
    return out


def _annotation_policy_value(src: bytes, ann_node) -> str:
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "string_literal":
            return _str_lit(src, c)
        if c.type == "element_value_pair":
            val = c.child_by_field_name("value")
            if val is not None and val.type == "string_literal":
                return _str_lit(src, val)
    return ""


def _java_event_consumer_topics(src: bytes, node) -> list[str]:
    out: list[str] = []
    for ann in _annotation_nodes(node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        if name not in _EVENT_CONSUMER_ANNOTATIONS:
            continue
        args = ann.child_by_field_name("arguments")
        if args is None:
            continue
        for c in args.children:
            if c.type == "string_literal":
                topic = _str_lit(src, c).strip()
                if topic:
                    out.append(topic)
            elif c.type == "element_value_pair":
                key = c.child_by_field_name("key")
                val = c.child_by_field_name("value")
                if key is None or val is None:
                    continue
                if text(src, key) in ("topic", "topics", "queue", "queues", "destination", "value"):
                    if val.type == "string_literal":
                        topic = _str_lit(src, val).strip()
                        if topic:
                            out.append(topic)
    dedup: list[str] = []
    for t in out:
        if t not in dedup:
            dedup.append(t)
    return dedup


def _types_in(src: bytes, node):
    """Collect simple type names under a superclass/interfaces node."""
    out = []
    for d in [node, *iter_descendants(node)]:
        if d.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            name = simple_type_name(text(src, d))
            if name and name not in out:
                out.append(name)
    return out


def _complexity_counts(body):
    if body is None:
        return 0, 0
    branch_nodes = {
        "if_statement",
        "switch_expression",
        "switch_block_statement_group",
        "conditional_expression",
        "catch_clause",
    }
    loop_nodes = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
    branch_count = 0
    loop_count = 0
    for n in iter_descendants(body):
        if n.type in branch_nodes:
            branch_count += 1
        if n.type in loop_nodes:
            loop_count += 1
    return branch_count, loop_count

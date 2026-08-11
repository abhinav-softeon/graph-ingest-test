"""JSP extraction — the entry-point layer of a Java web app.

A .jsp is a template that Jasper compiles into a servlet. Rather than reimplement
call/import/instantiation detection for a second Java dialect, this translates
the page into a synthetic Java compilation unit, hands it to the existing Java
extractor, and then remaps every line number back onto the .jsp.

    <%@ page import="com.acme.Svc" %>     ->   import com.acme.Svc;
    <%! private int hits; %>              ->   class-level member
    <% svc.load(id); %>                   ->   statement in _jspService()
    <%= svc.getName() %>                  ->   _jspx_out.print( ... );

Everything java.py knows how to find — CALLS, IMPORTS, INSTANTIATES, field
access, annotations — therefore works on JSP for free, and reports positions a
human can actually open.

WHY THIS MATTERS MORE THAN ITS FILE COUNT SUGGESTS
JSP is where a request enters the system. Without it, every call path starts
somewhere in the middle of the business logic and the question "which screen
triggers this database write?" has no answer. A JSP-less graph of a JSP app is
missing its whole top layer.

SCRIPTLETS SPLIT ACROSS SEGMENTS ARE NORMAL
    <% if (ok) { %> <p>hi</p> <% } %>
Each scriptlet is a fragment, not a statement. Concatenating them in page order
reassembles the block, which is exactly what Jasper does. Where a page still
does not parse cleanly, tree-sitter's error recovery keeps whatever it can and
the rest of the page is unaffected.

NOT COVERED
Custom tag handlers (needs the .tld), EL expressions (``${user.name}`` compiles
to a getter call, but binding it correctly needs the bean's declared type), and
anything Jasper generates rather than the author writing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..discovery import FileInfo
from ..ids import make_id
from ..models import Node, RawRef
from . import java as _java

EXTRACTOR = "tree-sitter-jsp"

# Longest opener first: '<%--' and '<%@' and '<%!' and '<%=' all start with '<%'.
_SEGMENTS = (
    ("comment", "<%--", "--%>"),
    ("directive", "<%@", "%>"),
    ("declaration", "<%!", "%>"),
    ("expression", "<%=", "%>"),
    ("scriptlet", "<%", "%>"),
)

# Receiver used to make `<%= expr %>` syntactically valid. It is scaffolding,
# not something the page's author wrote, so the call it produces is dropped
# before the refs leave this module — otherwise every expression in every page
# emits a CALLS to a `print` that does not exist.
_OUT_SENTINEL = "_jspx_out"

_ATTR_RE = re.compile(r"""(\w[\w:-]*)\s*=\s*("[^"]*"|'[^']*')""")
_JSP_ACTION_RE = re.compile(
    r"<jsp:(include|forward|useBean|directive\.include)\b([^>]*)/?>", re.IGNORECASE)
_EL_RE = re.compile(r"[$#]\{([^}]*)\}")
# `cart.total` inside an EL expression. Only the FIRST hop is bound: in
# `${order.customer.name}` the type of `order` is known from useBean, but
# `getCustomer()`'s return type is not available without resolving it, so
# guessing at `name` would invent an edge.
_EL_PROPERTY_RE = re.compile(r"\b(\w+)\.(\w+)")
_IDENT_SAFE_RE = re.compile(r"[^0-9A-Za-z_]")

# `<script src="...">`, any case, quoted or bare. The page's client-side half.
_SCRIPT_SRC_RE = re.compile(
    r"""<\s*script\b[^>]*?\bsrc\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE | re.DOTALL,
)
# A .js filename inside that attribute. Extracted as a TOKEN rather than taking
# the attribute whole, because the attribute is almost never a plain path:
#
#   <SCRIPT src="<%=BaseConstant.BASE_SCRIPT_ADHOC_URL%>quicksearch.js<%=BROWSER_CACHE_VERSION%>">
#
# The directory comes from a Java constant and a cache-buster follows the
# extension, so the only stable part is the filename in the middle. Matching
# `[\w.-]+\.js` picks exactly that and ignores both `<%= %>` blocks.
_JS_FILE_RE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.\-]*\.js)\b")


def _attrs(raw: str) -> dict:
    return {m.group(1).lower(): m.group(2)[1:-1] for m in _ATTR_RE.finditer(raw)}


@dataclass
class JspTranslation:
    """Synthetic Java for a page, plus the mapping needed to undo it."""
    java_source: str = ""
    # synthetic 1-based line -> .jsp 1-based line. Complete: every emitted line
    # has an entry, so remapping never has to guess.
    line_map: dict = field(default_factory=dict)
    imports: list = field(default_factory=list)
    includes: list = field(default_factory=list)    # (path, jsp_line, kind)
    beans: list = field(default_factory=list)       # (id, class_name, jsp_line)
    taglibs: list = field(default_factory=list)     # (prefix, uri, jsp_line)
    scripts: list = field(default_factory=list)     # (js_filename, jsp_line)
    el_calls: list = field(default_factory=list)    # (receiver, class, getter, jsp_line)
    el_expressions: int = 0
    scriptlet_lines: int = 0


def _scan(source: str):
    """Yield (kind, body, start_line) for each JSP segment, in page order."""
    pos = 0
    line = 1
    length = len(source)
    while pos < length:
        best = None
        for kind, opener, closer in _SEGMENTS:
            found = source.find(opener, pos)
            if found != -1 and (best is None or found < best[1]):
                best = (kind, found, opener, closer)
        if best is None:
            break
        kind, start, opener, closer = best
        line += source.count("\n", pos, start)
        body_start = start + len(opener)
        end = source.find(closer, body_start)
        if end == -1:                       # unterminated — take the rest
            end = length
        body = source[body_start:end]
        if kind != "comment":
            yield kind, body, line + source.count("\n", start, body_start)
        line += source.count("\n", start, end + len(closer))
        pos = end + len(closer)


def _class_identity(relpath: str) -> tuple[str, str]:
    """(package, class_name) for a page, mirroring Jasper's naming intent.

    The directory becomes a package so two pages both called ``index.jsp`` in
    different folders do not collide on one fqn. Segments are sanitized because
    real webapp paths (``WEB-INF``) are not legal Java identifiers.
    """
    posix = relpath.replace("\\", "/")
    directory, filename = os.path.split(posix)
    base = os.path.splitext(filename)[0]
    class_name = _IDENT_SAFE_RE.sub("_", base) + "_jsp"
    if class_name[0].isdigit():
        class_name = "_" + class_name
    parts = []
    for seg in directory.split("/"):
        if not seg or seg in (".", ".."):
            continue
        safe = _IDENT_SAFE_RE.sub("_", seg)
        if safe and safe[0].isdigit():
            safe = "_" + safe
        if safe:
            parts.append(safe)
    return ".".join(parts), class_name


def translate(source: str, relpath: str) -> JspTranslation:
    """Turn a JSP page into a synthetic Java compilation unit."""
    tr = JspTranslation()
    package, class_name = _class_identity(relpath)

    out: list[str] = []

    def emit(text: str, jsp_line: int) -> None:
        for offset, chunk in enumerate(text.split("\n")):
            out.append(chunk)
            tr.line_map[len(out)] = max(1, jsp_line + offset)

    declarations: list[tuple[str, int]] = []
    body: list[tuple[str, int]] = []

    for kind, raw, jsp_line in _scan(source):
        if kind == "directive":
            directive = raw.strip()
            attrs = _attrs(directive)
            low = directive.lower()
            if low.startswith("page"):
                for spec in attrs.get("import", "").split(","):
                    spec = spec.strip()
                    if spec:
                        tr.imports.append((spec, jsp_line))
            elif low.startswith("include"):
                target = attrs.get("file", "")
                if target:
                    tr.includes.append((target, jsp_line, "static"))
            elif low.startswith("taglib"):
                tr.taglibs.append((attrs.get("prefix", ""), attrs.get("uri", ""), jsp_line))
        elif kind == "declaration":
            declarations.append((raw, jsp_line))
            tr.scriptlet_lines += raw.count("\n") + 1
        elif kind == "scriptlet":
            body.append((raw, jsp_line))
            tr.scriptlet_lines += raw.count("\n") + 1
        elif kind == "expression":
            expr = raw.strip()
            if expr and ";" not in expr:
                # A JSP expression is an expression, never a statement; Jasper
                # wraps it in a print call and so do we, so tree-sitter sees
                # something syntactically valid. The wrapper call itself is
                # filtered back out in extract().
                body.append((f"{_OUT_SENTINEL}.print({expr});", jsp_line))

    for match in _JSP_ACTION_RE.finditer(source):
        action = match.group(1).lower()
        attrs = _attrs(match.group(2))
        jsp_line = source.count("\n", 0, match.start()) + 1
        if action in ("include", "directive.include"):
            target = attrs.get("page") or attrs.get("file")
            if target:
                tr.includes.append((target, jsp_line, "dynamic"))
        elif action == "forward":
            target = attrs.get("page")
            if target:
                tr.includes.append((target, jsp_line, "forward"))
        elif action == "usebean":
            cls = attrs.get("class") or attrs.get("type")
            if cls:
                tr.beans.append((attrs.get("id", ""), cls, jsp_line))

    # `<script src>` — the JSP -> JS hop. Scanned off the RAW MARKUP, not the
    # synthetic Java, because a script tag is markup: it never reaches the
    # translation unit, so nothing downstream would ever see it. This module read
    # only `<% %>` segments before, which is why a page's JS was invisible.
    seen_scripts: set = set()
    for match in _SCRIPT_SRC_RE.finditer(source):
        attr = match.group(1).strip("\"'")
        jsp_line = source.count("\n", 0, match.start()) + 1
        for js_name in _JS_FILE_RE.findall(attr):
            if js_name not in seen_scripts:
                seen_scripts.add(js_name)
                tr.scripts.append((js_name, jsp_line))

    # EL property access is a real getter call at runtime: `${cart.total}`
    # invokes cart.getTotal(). Bound only when the receiver is a declared bean,
    # so the call carries a recv_type and lands on the resolver's typed path
    # instead of fanning out across every getTotal in the repo.
    bean_types = {bean_id: cls for bean_id, cls, _ln in tr.beans if bean_id}
    for match in _EL_RE.finditer(source):
        tr.el_expressions += 1
        jsp_line = source.count("\n", 0, match.start()) + 1
        for receiver, prop in _EL_PROPERTY_RE.findall(match.group(1)):
            cls = bean_types.get(receiver)
            if cls and prop:
                # java.py stores recv_type as a SIMPLE name (via
                # simple_type_name), and the resolver's classes_by_name index is
                # keyed the same way. useBean declares a fully-qualified class,
                # so it has to be reduced or every EL call would miss the typed
                # path and fall back to global name matching.
                tr.el_calls.append((
                    receiver, cls.split("<", 1)[0].rsplit(".", 1)[-1],
                    f"get{prop[0].upper()}{prop[1:]}", jsp_line,
                ))

    # --- assemble the compilation unit ---------------------------------
    if package:
        emit(f"package {package};", 1)
    for spec, jsp_line in tr.imports:
        emit(f"import {spec};", jsp_line)
    emit(f"public class {class_name} {{", 1)
    for raw, jsp_line in declarations:
        emit(raw, jsp_line)
    # `<jsp:useBean class="com.acme.Cart" id="cart"/>` really does construct one.
    # Declaring it here gives java.py both the INSTANTIATES and the local
    # variable type, so later `cart.checkout()` calls carry a recv_type.
    emit("  public void _jspService() {", 1)
    for bean_id, cls, jsp_line in tr.beans:
        if bean_id and cls:
            emit(f"    {cls} {bean_id} = new {cls}();", jsp_line)
    for raw, jsp_line in body:
        emit(raw, jsp_line)
    emit("  }", 1)
    emit("}", 1)

    tr.java_source = "\n".join(out)
    return tr


def _remap_line(line_map: dict, line: int, fallback: int = 1) -> int:
    if line in line_map:
        return line_map[line]
    # Synthetic lines always have an entry; a miss means a position past the end
    # (tree-sitter can report end_point one past the last line). Use the closest
    # preceding real mapping rather than inventing a number.
    for probe in range(line - 1, 0, -1):
        if probe in line_map:
            return line_map[probe]
    return fallback


def extract(file: FileInfo, repo: str):
    """(nodes, edges, refs) for a JSP page."""
    try:
        source = file.source.decode("utf-8", "replace")
    except Exception:
        return [], [], []

    tr = translate(source, file.relpath)
    jsp_lines = source.count("\n") + 1
    _package, class_name = _class_identity(file.relpath)

    shim = FileInfo(
        relpath=file.relpath, abspath=file.abspath, lang="java",
        sha=file.sha, source=tr.java_source.encode("utf-8"),
    )
    try:
        nodes, edges, refs = _java.extract(shim, repo)
    except Exception:
        # A page that will not parse must not take the run down with it; the
        # File node below still lands so the page exists in the graph.
        nodes, edges, refs = [], [], []

    for node in nodes:
        node.lang = "jsp"
        node.extractor = EXTRACTOR
        node.start_line = _remap_line(tr.line_map, node.start_line)
        node.end_line = max(node.start_line, _remap_line(tr.line_map, node.end_line, jsp_lines))
        # The page class and its service method are scaffolding this module
        # invented; their synthetic braces sit on generated lines that map back
        # to line 1. Semantically they ARE the page, so they span it — otherwise
        # both collapse to L1-1 and neither can be opened usefully.
        if node.label == "File" or node.name in (class_name, "_jspService"):
            node.start_line, node.end_line = 1, jsp_lines
        if node.label == "File":
            node.body_hash = file.sha
    for edge in edges:
        if edge.evidence_line:
            edge.evidence_line = _remap_line(tr.line_map, edge.evidence_line)
    refs = [r for r in refs if r.recv != _OUT_SENTINEL]
    for ref in refs:
        if ref.ref_line:
            ref.ref_line = _remap_line(tr.line_map, ref.ref_line)

    file_id = make_id(repo, file.relpath, "file")
    if not any(n.label == "File" for n in nodes):
        nodes.append(Node(
            id=file_id, label="File", name=os.path.basename(file.relpath),
            fqn=file.relpath, repo=repo, kind="file", lang="jsp",
            file=file.relpath, start_line=1, end_line=jsp_lines,
            body_hash=file.sha, extractor=EXTRACTOR,
        ))

    # Page-to-page navigation. Emitted as refs rather than edges because the
    # target file's node id is not knowable from inside one file's extraction;
    # the resolver matches File nodes by name, and an include that resolves to
    # two same-named pages is honestly ambiguous rather than silently wrong.
    # EL property reads. Emitted here rather than through the synthetic Java so
    # the line number is the EL's own, not wherever a generated call would have
    # landed — `${cart.total}` sits in the middle of markup, not in a scriptlet.
    service_id = ""
    for node in nodes:
        if node.label == "Function" and node.name == "_jspService":
            service_id = node.id
            break
    if service_id:
        for receiver, cls, getter, jsp_line in tr.el_calls:
            refs.append(RawRef(
                "CALLS", service_id, getter, "call", recv=receiver, recv_type=cls,
                call_arity=0, ref_file=file.relpath, ref_line=jsp_line, ref_col=0,
            ))

    for target, jsp_line, _kind in tr.includes:
        name = os.path.basename(target.split("?", 1)[0].replace("\\", "/"))
        if name:
            refs.append(RawRef(
                "INCLUDES_PAGE", file_id, name, "file",
                ref_file=file.relpath, ref_line=jsp_line, ref_col=0,
            ))

    # File -> File, not Function -> File: a script tag belongs to the page, not
    # to any one scriptlet, and the browser loads it for the whole page.
    for js_name, jsp_line in tr.scripts:
        refs.append(RawRef(
            "INCLUDES_SCRIPT", file_id, js_name, "file",
            ref_file=file.relpath, ref_line=jsp_line, ref_col=0,
        ))

    return nodes, edges, refs

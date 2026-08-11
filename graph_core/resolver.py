"""Stage 2 (Phase-0 heuristic) — resolve name-based RawRefs to edges.

A real call graph needs scip-java/Pyright. This first pass resolves by name
within the repo, but uses cheap lexical scope to cut ambiguity for Python:

  (a) `self.method()` / `cls.method()` → methods of the enclosing class.
  (b) `Class.method()`                 → methods of that in-repo class.
  (c) bare `func()`                    → prefer a same-file definition before
                                         falling back to the global name index.

Unique match -> INFERRED edge; multiple -> AMBIGUOUS edges to all candidates;
no match -> unresolved (counted, no edge). Annotations are materialized as
:Annotation nodes keyed by name (EXTRACTED: the usage is observed directly).
"""
from __future__ import annotations

import gc
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .apispec import (
    endpoint_display,
    endpoint_fqn,
    endpoint_id,
    match_key,
    normalize_route,
)
from .checkpoint import load_resolve_checkpoint, save_resolve_checkpoint
from .config import resolve_checkpoint_seconds
from .catalog import classify_taint
from .external_api import (
    classify_call, external_display, external_id, external_key,
)
from .config import name_match_max_candidates
from .ids import make_id
from .models import Confidence, Edge, IngestCancelled, Node, Origin, RawRef
from .schema import DROPPED_EDGE_TYPES, NOISE_ANNOTATIONS
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Enum member `.value` goes through Enum's descriptor protocol on EVERY access —
# a Python-level __get__ call, not a plain attribute load. These are read once
# or twice per emitted edge, so at multi-million-edge scale they were measured
# at ~1.18M descriptor calls and ~13% of resolve's total time, purely to fetch
# constant strings. Bound once here; the values are identical, so output is
# unchanged.
_CONF_EXTRACTED = Confidence.EXTRACTED.value
# Verb used by endpoints that serve every HTTP method (@WebServlet). Mirrors
# extractors.java.SERVLET_ANY_METHOD; kept as a local constant so the resolver
# does not import an extractor.
_ANY_METHOD = "ANY"
# Which File languages each cross-language file reference may resolve to. The
# filter is the precision mechanism: without it `common.jsp` and `common.js`
# compete for the same basename.
_FILE_REF_LANGS = {
    "RENDERS": frozenset({"jsp"}),
    "INCLUDES_SCRIPT": frozenset({"javascript", "typescript", "tsx"}),
    "INCLUDES_PAGE": frozenset({"jsp"}),
}
_CONF_INFERRED = Confidence.INFERRED.value
_CONF_AMBIGUOUS = Confidence.AMBIGUOUS.value
_ORIGIN_EXTRACTED = Origin.EXTRACTED.value


@dataclass
class Coverage:
    total: int = 0
    resolved: int = 0     # unique match
    ambiguous: int = 0    # >1 candidate
    unresolved: int = 0   # 0 candidates but the name DOES exist in-repo (a real miss)
    external: int = 0     # 0 candidates and the name is unknown in-repo (stdlib/3rd-party/builtin)

    @property
    def inrepo(self) -> int:
        """Refs that target something nameable in this repo (excludes external)."""
        return self.resolved + self.ambiguous + self.unresolved

    def pct(self) -> float:
        """Honest resolution rate: resolved as a share of in-repo targets."""
        return 100.0 * self.resolved / self.inrepo if self.inrepo else 0.0


def resolve(
    nodes: list[Node], edges: list[Edge], refs: list[RawRef], repo: str,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    checkpoint_root: str | None = None,
    edge_sink: "Callable[[list[Node], list[Edge]], list] | None" = None,
    skip_call_files: set[str] | None = None,
    taint_marks: dict | None = None,
):
    """Return (extra_nodes, edges, coverage_by_reftype).

    ``skip_call_files``: repo-relative files whose CALLS a type-precise resolver
    already handled (graph_core/javac_resolver.py's ``attributed_files``). Their
    CALLS refs are skipped rather than resolved and later discarded — producing
    them is the expensive part, and the precise resolver's answer supersedes
    them anyway.

    Deliberately keyed on FILE, not language: javac may attribute only part of a
    tree, and anything it missed must still get heuristic edges or those files
    would end up with no call graph at all. Per-file makes partial coverage
    degrade gracefully instead of falling off a threshold. Every other ref type
    in those files still resolves here — only CALLS is handed over.

    ``taint_marks``: optional dict, FILLED IN PLACE with
    {caller_id: {"cats": set, "src": bool, "sites": [...]}} for catalogued
    source/sink calls on the external path. An out-param rather than a fourth
    return value so every existing caller and test keeps unpacking three.

    It cannot be applied to the nodes here: by the time resolve() runs they are
    already the SlimNode projection (pipeline converts them right after the
    pre-resolve write), and SlimNode has no taint fields. The pipeline writes
    these straight to Neo4j instead, where the nodes already exist.

    `edges` is the structural edge list from extraction (CONTAINS), used to
    build the containment scope that powers scope-aware call resolution.

    ``edge_sink(new_nodes, edge_batch) -> replacement_batch``: optional
    (A+B Step 11, TIER3_MEMORY_PLAN.md §11). When given, every ~_SINK_BATCH
    resolved edges are handed to the sink, which may persist them (to Neo4j)
    and return lighter stand-ins (SlimEdge) to retain in their place — so the
    full-object edge list never accumulates for the whole repo. ``new_nodes``
    is the slice of synthesized extra_nodes created since the previous flush:
    the sink MUST write those before the batch's edges, since a batch edge may
    point at a just-synthesized node that isn't in Neo4j yet. Emission order
    is preserved (batches are contiguous slices), so MERGE last-write-wins
    for duplicate (type, src, dst) keys matches the unsinked path exactly.
    Mutually exclusive with ``checkpoint_root`` — see the note at the
    ``edge_sink is not None`` check below for why resume cannot currently be
    made sound on the streaming path.

    ``on_progress(done, total)``: optional heartbeat callback. Fired once
    right before the main ref-resolution loop starts (covering the index-
    building phase above, and resolve-checkpoint loading if resumed — that
    window has no progress signal of its own otherwise), then periodically
    during the loop itself (this loop can be the single longest-running step
    for very large repos — e.g. ~4.9M refs — and previously had zero progress
    reporting, which could make a genuinely still-running job look silently
    dead).

    ``cancel_check()``: optional callback checked periodically in the same
    loop; if it returns True, raises ``IngestCancelled`` immediately rather
    than returning partial results as if resolution had completed normally —
    a caller silently treating a cancelled, partially-resolved graph as done
    would end up writing incomplete data to Neo4j and marking it "ready" as
    a future diff baseline. Only affects behavior when cancellation is
    actually requested — otherwise identical to before.

    ``checkpoint_root``: optional. If set, this loop periodically persists
    its progress (next ref index, accumulated output, dedup maps, coverage)
    to disk so a crash mid-resolve can resume from the last checkpoint
    instead of reprocessing every ref (this loop can run for hours on a
    multi-million-ref repo). Only affects WHERE/WHEN state is durable; the
    matching/resolution logic itself is unchanged.
    """
    _log.info(
        "[resolve][repo=%s] building lookup indices over %s node(s), %s edge(s), %s ref(s)",
        repo, len(nodes), len(edges), len(refs),
    )
    _t_index_start = time.monotonic()
    # Hoisted to a local: read per CALLS ref in the hot loop, and an empty set
    # short-circuits before the membership test on the default path.
    _skip_files = skip_call_files or frozenset()
    nodes_by_id: dict[str, Node] = {n.id: n for n in nodes}

    def _class_package(n: Node) -> str:
        fqn = n.fqn or ""
        name = n.name or ""
        if fqn == name or not fqn.endswith(f".{name}"):
            return ""
        return fqn[: -(len(name) + 1)]

    by_name: dict[str, list[Node]] = defaultdict(list)
    classes_by_name: dict[str, list[Node]] = defaultdict(list)
    # Function-only view of by_name, precomputed rather than rebuilt per ref.
    # narrow_call's pool used to be `[c for c in base if c.label == "Function"]
    # or base` — a fresh list allocation on EVERY call ref, scanning every
    # same-named node. For a hot name (`getId` in a Java repo can have tens of
    # thousands of definitions) that allocation alone dominated. Same pool,
    # same order, built once: `funcs_by_name.get(name) or by_name.get(name, [])`
    # is exactly the old expression, since `or` falls through on an absent or
    # empty function list identically.
    funcs_by_name: dict[str, list[Node]] = defaultdict(list)
    files_by_basename: dict[str, list[Node]] = defaultdict(list)
    # Precomputed once per Class/Function node here, instead of recomputed via
    # string-split/slice on every candidate on every ambiguous ref inside
    # narrow_call/narrow_type/_narrow_classes_for_recv below. For a Class node
    # this yields its package; for a Function/method node it yields the owning
    # class's FQN (both call sites below feed it either kind).
    class_package_by_id: dict[str, str] = {}
    for n in nodes:
        if n.label in ("Class", "Function"):
            by_name[n.name].append(n)
            class_package_by_id[n.id] = _class_package(n)
            if n.label == "Function":
                funcs_by_name[n.name].append(n)
        if n.label == "Class":
            classes_by_name[n.name].append(n)
        elif n.label == "File":
            # Basename -> File nodes, for the cross-language file references
            # (RENDERS / INCLUDES_SCRIPT / jsp:include). A basename is all the
            # source ever carries: the paths are built as
            # `CONSTANT_PREFIX + "page.jsp"`, so the directory lives in a Java or
            # JS constant that is not resolvable here. Node.name is already the
            # basename for every extractor.
            files_by_basename[n.name].append(n)

    # Class fqn -> Class node, for a target that arrives fully qualified (a JS
    # HANDLED_BY literal naming its handler outright). Exact beats the simple-name
    # index, which cannot separate two same-named classes in different packages.
    classes_by_fqn: dict[str, Node] = {}
    for n in nodes:
        if n.label == "Class" and n.fqn:
            classes_by_fqn.setdefault(n.fqn, n)

    # Read once, not per ref: resolve() handles millions of them.
    _name_cap = name_match_max_candidates()

    # Containment: child -> parent, and class -> {method_name: [nodes]}.
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src

    imports_by_file: dict[str, set[str]] = defaultdict(set)
    import_fqns_by_file: dict[str, set[str]] = defaultdict(set)
    # Java-only: package-prefixes brought in via a wildcard import
    # (`import a.b.*;`) — kept separate from import_fqns_by_file since a
    # wildcard's import_fqn is a PACKAGE prefix, not a class FQN, and mixing
    # it into the qualified-class matcher below would risk matching an
    # unrelated same-tail-segment class name.
    wildcard_pkgs_by_file: dict[str, set[str]] = defaultdict(set)
    # Collected here rather than in a scan of their own: the ancestor pre-pass
    # below needs every EXTENDS/IMPLEMENTS ref, and this loop is already walking
    # all of them (~5M on the measured repo). Picking them up in passing keeps
    # that to one traversal. The list itself is tiny — HANDOFF measured 8,320
    # EXTENDS + 1,138 IMPLEMENTS.
    hierarchy_refs: list[RawRef] = []
    for ref in refs:
        if ref.type in ("EXTENDS", "IMPLEMENTS"):
            hierarchy_refs.append(ref)
            continue
        if ref.type != "IMPORTS" or not ref.ref_file:
            continue
        if ref.kind_hint == "import_wildcard":
            if ref.import_fqn:
                wildcard_pkgs_by_file[ref.ref_file].add(ref.import_fqn)
            continue
        if ref.target_name:
            imports_by_file[ref.ref_file].add(_tail_name(ref.target_name))
        if ref.import_fqn:
            import_fqns_by_file[ref.ref_file].add(ref.import_fqn)

    # Import tails per file, for _import_qualified_hits. Derived once per FILE
    # here because that is what it depends on — it used to be recomputed inside
    # _import_qualified_hits on every call, i.e. once per ref in the file, so a
    # file with 40 imports re-tailed all 40 for every one of its call sites.
    import_tails_by_file: dict[str, frozenset] = {}
    for _f, _fqns in import_fqns_by_file.items():
        _t = {_tail_name(i) for i in _fqns}
        _t.discard("")
        if _t:
            import_tails_by_file[_f] = frozenset(_t)
    # Lazily-filled fqn-segment memo shared by every _import_qualified_hits
    # call (see its docstring for why it is lazy rather than precomputed).
    fqn_segs_cache: dict[str, frozenset] = {}
    _EMPTY_TAILS: frozenset = frozenset()

    # Java-only: package of the File a class/node lives in (from the File
    # node's `package` attribute, set by the Java extractor) — used for
    # same-package auto-visibility (no import needed to see a sibling class
    # in the same package, the most common intra-package call pattern).
    package_by_file: dict[str, str] = {
        n.file: (n.package or "") for n in nodes if n.label == "File" and n.file
    }

    # Endpoint index for CALLS_API matching. Exact key first; templated routes
    # (segments collapsed to `*`) matched segment-wise so a concrete caller path
    # `/api/users/42` resolves to the server's `/api/users/{id}`.
    endpoints_by_key: dict[tuple[str, str], list[Node]] = defaultdict(list)
    endpoint_patterns: dict[str, list[tuple[list[str], Node]]] = defaultdict(list)
    # Route -> endpoints, verb ignored. Backs match_endpoints' verb-agnostic
    # fallback with a dict probe instead of a walk over endpoints_by_key.
    endpoints_by_route: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        if n.label != "Endpoint":
            continue
        method, route = match_key(n.method, n.route)
        endpoints_by_key[(method, route)].append(n)
        endpoints_by_route[route].append(n)
        if "*" in route:
            endpoint_patterns[method].append((_route_segments(route), n))

    def match_endpoints(method: str, route: str) -> list[Node]:
        exact = endpoints_by_key.get((method, route))
        if exact:
            return exact
        # Verb-agnostic fallback. A servlet registered by @WebServlet serves EVERY
        # verb, so it is stored under ANY (see java.py SERVLET_ANY_METHOD); a
        # caller that knows its verb would miss it on the exact key above, and a
        # caller that does not know its verb sends ANY and would miss a
        # verb-specific Spring route. Trying both directions is what lets one
        # index hold both conventions.
        if method != _ANY_METHOD:
            any_hit = endpoints_by_key.get((_ANY_METHOD, route))
            if any_hit:
                return any_hit
        else:
            # Precomputed, NOT a scan over endpoints_by_key: this runs once per
            # CALLS_API ref, and a repo with thousands of servlets would turn a
            # dict probe into a full index walk per call site.
            any_route = endpoints_by_route.get(route)
            if any_route:
                return any_route
        caller = _route_segments(route)
        hits = []
        for segs, ep in endpoint_patterns.get(method, []):
            if len(segs) == len(caller) and all(
                ps == "*" or ps == cs for ps, cs in zip(segs, caller)
            ):
                hits.append(ep)
        return hits

    # (owner_id, member_name) -> Node, or list[Node] when that name is genuinely
    # overloaded/duplicated within the owner. Flat tuple key + scalar value,
    # measured at ~40 B/entry against ~311 B for the dict-of-dicts-of-lists these
    # replace: at one method per (class, name) — overwhelmingly the common case —
    # the old shape paid for an inner dict AND a one-element list per member.
    # Values stay ordered exactly as the node iteration produced them, so the
    # candidate lists handed to _apply_arity are unchanged.
    #
    # Also fixes an incidental leak: these were defaultdicts read as
    # `methods_of_class[cls_id]`, which MINTED an empty inner dict for every
    # class-id probed and missed. Plain dict + .get() cannot.
    methods_of_class: dict[tuple[str, str], object] = {}
    fields_of_class: dict[tuple[str, str], object] = {}
    fields_of_file: dict[tuple[str, str], object] = {}

    # Composite scope indices for narrow_call steps (1) and (2). Those steps
    # filtered the ENTIRE same-name pool linearly on one equality each
    # (`parent_of[c.id] == src_parent`, `c.file == src_file`), so their cost
    # scaled with the number of same-named symbols in the repo — which itself
    # scales with repo size. That made resolve quadratic in codebase size:
    # measured 4.2 us/ref at 3.4k nodes rising to 51.4 us/ref at 55k nodes,
    # doubling per doubling. Keying the equality directly turns each step into
    # one dict probe, at ~212 B/node for the pair. Same scalar-or-list shape as
    # methods_of_class above, and built in node iteration order so the
    # candidate lists stay ordered exactly as pool-filtering produced them.
    by_name_parent: dict[tuple[str, str], object] = {}
    by_name_file: dict[tuple[str, str], object] = {}

    def _index_member(idx: dict, key: tuple[str, str], node: Node) -> None:
        cur = idx.get(key)
        if cur is None:
            idx[key] = node
        elif isinstance(cur, list):
            cur.append(node)
        else:
            idx[key] = [cur, node]

    for n in nodes:
        if n.label == "Field" and n.scope == "module" and n.file:
            _index_member(fields_of_file, (n.file, n.name), n)
        p = parent_of.get(n.id)
        # Before the class-parent guard below: these two cover every
        # Class/Function node, whatever its container is (file, function,
        # module), because narrow_call's pool does too.
        if n.label in ("Class", "Function"):
            _index_member(by_name_parent, (n.name, p), n)
            if n.file:
                _index_member(by_name_file, (n.name, n.file), n)
        if not (p and nodes_by_id.get(p) and nodes_by_id[p].label == "Class"):
            continue
        if n.label == "Function" and n.kind == "method":
            _index_member(methods_of_class, (p, n.name), n)
        elif n.label == "Field":
            _index_member(fields_of_class, (p, n.name), n)

    def _members(idx: dict, key: tuple[str, str]) -> list[Node]:
        """Candidates for (owner, name) as a list, whatever shape is stored."""
        cur = idx.get(key)
        if cur is None:
            return []
        return cur if isinstance(cur, list) else [cur]

    def enclosing_class_id(node_id: str) -> str | None:
        cur = parent_of.get(node_id)
        while cur is not None:
            cn = nodes_by_id.get(cur)
            if cn is None:
                break
            if cn.label == "Class":
                return cur
            cur = parent_of.get(cur)
        return None

    def _narrow_classes_for_recv(candidates: list[Node], src_file: str) -> list[Node]:
        """Given >1 class sharing a simple name (e.g. two `PathUtil`s in
        different packages), narrow to the one(s) actually visible from
        `src_file` using the SAME precedence `narrow_type` uses for
        EXTENDS/IMPLEMENTS/INSTANTIATES/AUTOWIRED: same-file > qualified
        import (exact fqn > tail) > same-package > wildcard import > plain
        imported simple name > give up (all).

        Found live: `narrow_call`'s receiver-type/receiver-class-name steps
        used to do `classes_by_name[name]` directly with NO package/import
        filtering, so a call like `PathUtil.normalize(x)` with two same-name
        `PathUtil` classes in different packages resolved to BOTH classes'
        methods — producing duplicate/ambiguous CALLS edges (and duplicate
        impact findings) even though only one was actually imported/visible.
        """
        if len(candidates) <= 1 or not src_file:
            return candidates
        same_file = [c for c in candidates if c.file == src_file]
        if same_file:
            return same_file
        imported_fqns = import_fqns_by_file.get(src_file, set())
        exact = [c for c in candidates if c.fqn in imported_fqns]
        if exact:
            return exact
        qualified_hits = _import_qualified_hits(
            candidates, import_tails_by_file.get(src_file, _EMPTY_TAILS), fqn_segs_cache,
        )
        if qualified_hits:
            return qualified_hits
        src_pkg = package_by_file.get(src_file, "")
        if src_pkg:
            same_pkg = [c for c in candidates if class_package_by_id.get(c.id, "") == src_pkg]
            if same_pkg:
                return same_pkg
        wpkgs = wildcard_pkgs_by_file.get(src_file, set())
        if wpkgs:
            wcard_hits = [c for c in candidates if class_package_by_id.get(c.id, "") in wpkgs]
            if wcard_hits:
                return wcard_hits
        imported = imports_by_file.get(src_file, set())
        imported_hits = [c for c in candidates if c.name in imported]
        if imported_hits:
            return imported_hits
        return candidates

    def narrow_call(ref: RawRef) -> tuple[list[Node], str]:
        """Best candidate set for a CALLS ref with deterministic strategy ordering."""
        name = ref.target_name
        # The receiver's type is KNOWN and is not a class we extracted: the
        # callee lives in a library/JDK type, so no in-repo function can be the
        # target. Every candidate below would be wrong by construction.
        #
        # Without this, such a call falls all the way through to the step (5)
        # arity fallback and fans out across every same-named function in the
        # repo — `conn.close()` matching every `close()` in the codebase,
        # `logger.info()` every `info()`. In a JDBC/collections-heavy Java repo
        # that is a large share of all call sites, and 100% of the edges it
        # produces are false. Returning no candidates costs nothing real: the
        # true target was never in the graph to find.
        #
        # Deliberately keyed on recv_type (the DECLARED type) rather than recv
        # (the variable name) — an unknown variable name could still be a static
        # call on an in-repo class, which the receiver-is-a-class-name step below
        # handles. Refs with no recv_type at all are untouched.
        if ref.recv_type and ref.recv_type not in classes_by_name:
            return [], "external_receiver"
        fns = funcs_by_name.get(name)
        pool = fns or by_name.get(name, [])
        if not pool:
            return [], "none"
        # Did the pool narrow to Functions? Steps (1)/(2) probe indices built
        # over all Class/Function nodes, so their hits need the same label
        # filter the old pool comprehension applied up front.
        fns_only = fns is not None
        src_node = nodes_by_id.get(ref.src)

        # (1) Same-scope preference: same immediate container (class/file/function).
        src_parent = parent_of.get(ref.src)
        if src_parent is not None:
            same_scope = _members(by_name_parent, (name, src_parent))
            if fns_only and same_scope:
                same_scope = [c for c in same_scope if c.label == "Function"]
            if same_scope:
                return _apply_arity(ref, same_scope, "same_scope")

        # (2) Same-file preference.
        if src_node is not None and src_node.file:
            same_file = _members(by_name_file, (name, src_node.file))
            if fns_only and same_file:
                same_file = [c for c in same_file if c.label == "Function"]
            if same_file:
                return _apply_arity(ref, same_file, "same_file")

        # (3) Import-aware narrowing by imported simple names and qualified imports.
        if src_node is not None and src_node.file:
            imported = imports_by_file.get(src_node.file, set())
            imported_fqns = import_fqns_by_file.get(src_node.file, set())
            # Exact owner-class match FIRST: `_import_qualified_hits` below is
            # a loose tail/namespace match on `pool`'s (methods') own FQN,
            # which can't disambiguate two same-name classes in different
            # packages that both define a same-name method — both methods'
            # FQNs end in "....PathUtil.normalize" regardless of package, so
            # the loose match would hit both. Checking the method's OWNING
            # CLASS fqn against the imported fqn exactly closes that gap.
            exact_owner_hits = [c for c in pool if class_package_by_id.get(c.id, "") in imported_fqns]
            if exact_owner_hits:
                return _apply_arity(ref, exact_owner_hits, "imports_qualified_exact")
            qualified_hits = _import_qualified_hits(
                pool, import_tails_by_file.get(src_node.file, _EMPTY_TAILS), fqn_segs_cache,
            )
            if qualified_hits:
                return _apply_arity(ref, qualified_hits, "imports_qualified")
            if imported:
                imported_hits = [
                    c for c in pool
                    if c.name in imported or _tail_name(c.fqn) in imported
                ]
                if imported_hits:
                    return _apply_arity(ref, imported_hits, "imports")

        # (4) Receiver-type narrowing.
        #
        # Each of the three receiver steps below tries the class's OWN methods
        # first and only then its inherited ones. That order is not incidental:
        # a subclass method shadows the ancestor's, so consulting the hierarchy
        # first would resolve overridden calls to the wrong declaration. The
        # inherited path is tagged `*_inherited` so it stays separable — its
        # precision is that of the receiver type AND of the hierarchy edge it
        # walked, which is a compiler fact where bytecode covered the class and a
        # narrowed name match elsewhere.
        if ref.recv_type and ref.recv_type in classes_by_name:
            src_file = src_node.file if src_node else ""
            narrowed = _narrow_classes_for_recv(classes_by_name[ref.recv_type], src_file)
            hits: list[Node] = []
            for ccls in narrowed:
                hits.extend(_members(methods_of_class, (ccls.id, name)))
            if hits:
                return _apply_arity(ref, hits, "receiver_type_hint")
            inherited = _inherited_members([c.id for c in narrowed], name)
            if inherited:
                return _apply_arity(ref, inherited, "receiver_type_hint_inherited")

        # self/cls dispatch -> a method of the enclosing class.
        if ref.recv in ("self", "cls"):
            cid = enclosing_class_id(ref.src)
            if cid is not None:
                m = _members(methods_of_class, (cid, name))
                if m:
                    return _apply_arity(ref, m, "receiver_type")
                inherited = _inherited_members([cid], name)
                if inherited:
                    return _apply_arity(ref, inherited, "receiver_type_inherited")

        # Receiver is an in-repo class name -> that class's methods.
        if ref.recv and ref.recv not in ("self", "cls") and ref.recv in classes_by_name:
            src_file = src_node.file if src_node else ""
            narrowed = _narrow_classes_for_recv(classes_by_name[ref.recv], src_file)
            hits: list[Node] = []
            for ccls in narrowed:
                hits.extend(_members(methods_of_class, (ccls.id, name)))
            if hits:
                return _apply_arity(ref, hits, "receiver_type")
            inherited = _inherited_members([c.id for c in narrowed], name)
            if inherited:
                return _apply_arity(ref, inherited, "receiver_type_inherited")

        # (5) Arity-only fallback if no stronger narrowing worked.
        return _apply_arity(ref, pool, "name")

    def narrow_type(ref: RawRef) -> tuple[list[Node], str]:
        """Best candidate set for a type-shaped ref (EXTENDS/IMPLEMENTS/
        INSTANTIATES/AUTOWIRED) with the SAME package/import-awareness
        `narrow_call` already gives CALLS — previously these fell
        straight to the generic global by-name lookup with zero scope
        narrowing at all, the single biggest precision gap for Java, where
        multiple classes sharing a simple name across different packages
        (e.g. several `Impl`/`Repository`/`Config` classes) is routine."""
        name = _tail_name(ref.target_name)
        pool = [c for c in classes_by_name.get(name, [])]
        if not pool:
            return [], "none"
        if len(pool) == 1:
            return pool, "unique"
        src_node = nodes_by_id.get(ref.src)
        src_file = src_node.file if src_node else ""

        # (1) Same-file preference (nested/sibling classes in one file).
        if src_file:
            same_file = [c for c in pool if c.file == src_file]
            if same_file:
                return same_file, "same_file"

        # (2) Qualified (single-class) imports — an explicit single-type
        # import SHADOWS a same-package class of the same simple name (Java
        # scoping rule), so this must be checked before same-package below.
        # Exact FQN match beats a bare tail/namespace match, since two
        # candidates can share the same tail.
        if src_file:
            imported_fqns = import_fqns_by_file.get(src_file, set())
            exact = [c for c in pool if c.fqn in imported_fqns]
            if exact:
                return exact, "imports_qualified_exact"
            qualified_hits = _import_qualified_hits(
                pool, import_tails_by_file.get(src_file, _EMPTY_TAILS), fqn_segs_cache,
            )
            if qualified_hits:
                return qualified_hits, "imports_qualified"

        # (3) Same-package auto-visibility (Java: no import needed).
        src_pkg = package_by_file.get(src_file, "") if src_file else ""
        if src_pkg:
            same_pkg = [c for c in pool if class_package_by_id.get(c.id, "") == src_pkg]
            if same_pkg:
                return same_pkg, "same_package"

        # (4) Wildcard imports (Java: `import a.b.*;`) — candidate's package
        # matches one of the wildcard-imported package prefixes.
        if src_file:
            wpkgs = wildcard_pkgs_by_file.get(src_file, set())
            if wpkgs:
                wcard_hits = [c for c in pool if class_package_by_id.get(c.id, "") in wpkgs]
                if wcard_hits:
                    return wcard_hits, "imports_wildcard"

        # (5) Plain imported simple-name fallback (works for Python too, and
        # is the ORIGINAL (pre-fix) behavior for Java as a last resort).
        if src_file:
            imported = imports_by_file.get(src_file, set())
            imported_hits = [c for c in pool if c.name in imported]
            if imported_hits:
                return imported_hits, "imports"

        # (6) Give up narrowing — global name match, still ambiguous if >1.
        return pool, "name"

    # ---- ancestor pre-pass (inheritance-aware call resolution) ---------
    # methods_of_class holds a class's OWN methods only, so a call to a method it
    # INHERITS finds nothing there and falls through to the arity-only fallback,
    # which fans out across every same-named method in the repo. HANDOFF measured
    # the scale of this: 8,236,060 of 8.38M REFERENCES carry
    # `name+arity+unknown_recv`, i.e. "receiver type known, method not found on
    # it" — which in an Abstract*/I*-heavy codebase is overwhelmingly inheritance,
    # not a genuine miss. Those all get demoted to weak REFERENCES edges today.
    #
    # This must be a PRE-pass, not part of the main loop. EXTENDS/IMPLEMENTS are
    # themselves RawRefs resolved in that same loop, so consulting a hierarchy
    # built as it goes would make a call's resolution depend on whether its
    # ancestor happened to be resolved first — results would vary with input
    # order. Two sources, cheap either way (HANDOFF: 8,320 EXTENDS + 1,138
    # IMPLEMENTS against ~5M refs):
    #   1. hierarchy edges a precise tier already produced. bytecode_resolver
    #      emits EXTENDS/IMPLEMENTS at EXTRACTED straight from the class header
    #      (JVMS 4.1 super_class/interfaces), so where bytecode covered a class
    #      its ancestry is a compiler fact, not a name match.
    #   2. unresolved EXTENDS/IMPLEMENTS refs, resolved here via narrow_type —
    #      the same import/package-aware narrowing those edges would get in the
    #      main loop, just run early. Only unique matches are taken: an ambiguous
    #      supertype would poison every inherited call under it.
    _t_anc = time.monotonic()
    direct_supers: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.type in ("EXTENDS", "IMPLEMENTS"):
            direct_supers[e.src].append(e.dst)
    _anc_from_edges = sum(len(v) for v in direct_supers.values())
    for ref in hierarchy_refs:
        src_cls = enclosing_class_id(ref.src) if ref.src not in nodes_by_id else ref.src
        if src_cls is None:
            continue
        src_node = nodes_by_id.get(src_cls)
        if src_node is None or src_node.label != "Class":
            continue
        cands, _strat = narrow_type(ref)
        # Exactly one candidate or nothing. A wrong supertype is worse here than
        # a missing one: it redirects every inherited call on the subclass.
        if len(cands) == 1 and cands[0].id != src_cls:
            if cands[0].id not in direct_supers[src_cls]:
                direct_supers[src_cls].append(cands[0].id)

    def _ancestor_chain(class_id: str) -> list[str]:
        """Ancestors nearest-first: direct supertypes, then theirs, and so on.

        Breadth-first, so a method declared on the immediate superclass wins over
        the same method further up — which is what Java dispatch does. `seen`
        also makes this safe against a cycle in a malformed hierarchy (invalid
        Java, but this runs on whatever was uploaded).
        """
        out: list[str] = []
        seen_c: set[str] = {class_id}
        frontier = list(direct_supers.get(class_id, ()))
        while frontier:
            nxt: list[str] = []
            for cid in frontier:
                if cid in seen_c:
                    continue
                seen_c.add(cid)
                out.append(cid)
                nxt.extend(direct_supers.get(cid, ()))
            frontier = nxt
        return out

    # Memoized: a deep hierarchy is walked once per class, not once per call site.
    _chain_cache: dict[str, list[str]] = {}

    def _inherited_members(class_ids: list[str], name: str) -> list[Node]:
        """Methods named `name` inherited by any of `class_ids`.

        Stops at the FIRST ancestor of each starting class that declares the
        name — the nearest declaration is the one a call actually dispatches to,
        and collecting matches from further up would recreate the same fan-out
        this exists to remove.
        """
        hits: list[Node] = []
        for cid in class_ids:
            chain = _chain_cache.get(cid)
            if chain is None:
                chain = _ancestor_chain(cid)
                _chain_cache[cid] = chain
            for anc in chain:
                found = _members(methods_of_class, (anc, name))
                if found:
                    hits.extend(found)
                    break
        return hits

    _log.info(
        "[resolve][repo=%s] ancestor index: %s class(es) with supertypes "
        "(%s edge-sourced, %s ref-resolved) in %.3fs",
        repo, len(direct_supers), _anc_from_edges,
        sum(len(v) for v in direct_supers.values()) - _anc_from_edges,
        time.monotonic() - _t_anc,
    )

    _log.info(
        "[resolve][repo=%s] lookup indices built in %.3fs", repo, time.monotonic() - _t_index_start,
    )

    # Observability for the two fan-out caps and the ancestor fallback. Without
    # these, both caps drop work SILENTLY — the coverage counters record it but
    # resolve() never logged coverage at all, so a cap could be discarding
    # millions of sites and nothing in the output would say so. A bounded
    # dict of counters rather than nonlocal ints, so the nested emit/resolve
    # helpers can increment without closure rebinding (and Cython compiles it
    # the same either way).
    stats_counters: dict[str, int] = {
        "cap_dropped_emit": 0,          # emit(): bare-name fan-out over the cap
        "cap_dropped_unknown_recv": 0,  # the demotion branch, previously uncapped
    }
    # Edges by strategy — HANDOFF lists this as a tracked metric and nothing
    # produced it. One dict increment per emitted edge (~70ns), which is the
    # cheapest possible place to get the tier breakdown that tells you whether
    # the ancestor fallback (`*_inherited`) and bytecode tiers actually fired.
    strategy_counts: dict[str, int] = defaultdict(int)
    # Which ref types the emit() cap actually cost an edge. Separate from
    # cap_dropped_emit's total because "N sites dropped" is unactionable without
    # knowing whether N is CALLS (real recall loss) or EXTENDS/INSTANTIATES (the
    # bare-name type tier, far cheaper to lose).
    cap_dropped_by_type: dict[str, int] = defaultdict(int)

    extra_nodes: list[Node] = []
    annotation_ids: dict[str, str] = {}
    event_ids: dict[str, str] = {}
    policy_ids: dict[str, str] = {}
    api_endpoint_ids: set[str] = set()
    external_ids: set[str] = set()
    out_edges: list[Edge] = []
    coverage: dict[str, Coverage] = defaultdict(Coverage)

    if edge_sink is not None:
        # A metadata-only checkpoint (record the ref index, rely on the edges
        # already being durable in Neo4j) was implemented here and REVERTED —
        # it silently lost derived edges. Resuming skips already-resolved refs,
        # so their edges exist only in Neo4j, not in the in-RAM edge list that
        # the derive passes (_derive_overrides,
        # _synthesize_polymorphic_calls — both need real Edge objects, not
        # MERGE-collapsed Neo4j relationships, e.g. for evidence_file/
        # evidence_line) consume; a resumed run produced fewer derived edges
        # than an uninterrupted one. A sound resume must spill the slim edges
        # to disk as resolve produces them and reload them before derive.
        # Until then, resolve restarts from ref 0 — extraction resume is
        # unaffected.
        checkpoint_root = None

    resume_index = 0
    if checkpoint_root:
        saved = load_resolve_checkpoint(checkpoint_root, repo, len(refs))
        if saved is not None:
            resume_index = saved["next_index"]
            extra_nodes = saved["extra_nodes"]
            annotation_ids = saved["annotation_ids"]
            event_ids = saved["event_ids"]
            policy_ids = saved["policy_ids"]
            api_endpoint_ids = saved["api_endpoint_ids"]
            # Absent under a metadata-only checkpoint — those edges are already
            # in Neo4j, so resume starts with an empty in-RAM list and only
            # accumulates edges for the refs it actually still has to resolve.
            out_edges = saved.get("out_edges") or []
            coverage = saved["coverage"]
            _log.info(
                "[resolve][repo=%s] resuming: %s/%s refs already processed",
                repo, resume_index, len(refs),
            )

    def make_edge(
        ref: RawRef,
        dst: str,
        confidence: str,
        strategy: str = "",
        edge_type: str | None = None,
    ) -> Edge:
        strategy_counts[strategy or "(none)"] += 1
        # Heuristic edges are still EXTRACTED-origin (the reference is observed in
        # source); the *resolution* uncertainty is carried by `confidence`.
        return Edge(
            edge_type or ref.type, ref.src, dst, confidence,
            origin=_ORIGIN_EXTRACTED, extractor="heuristic",
            evidence_file=ref.ref_file, evidence_line=ref.ref_line,
            evidence_col=ref.ref_col,
            strategy=strategy,
        )

    def emit(ref: RawRef, cov: Coverage, wanted: list[Node], confidence: str,
             known_in_repo: bool, strategy: str = ""):
        if len(wanted) == 1:
            out_edges.append(make_edge(ref, wanted[0].id, confidence, strategy=strategy))
            cov.resolved += 1
        elif len(wanted) > 1:
            # AMBIGUITY CAP, bare-name tier only. A bare name matching N same-named
            # declarations emits N edges of which exactly ONE can be right, so the
            # fan-out is (N-1)/N false by construction. Measured: a JS helper named
            # `specialcheck` declared in ~20 files, called from ~11,432 sites, was
            # producing ~228k edges to carry ~11k real ones.
            #
            # Scoped to strategy `name*` deliberately. That is the tier with no
            # scope, no import and no receiver-type evidence — the ~5%-precision
            # bucket. same_scope/same_file/imports/receiver_type hits are
            # trustworthy and are never capped, however many candidates they have.
            #
            # Counted as unresolved, which is the honest bucket: the name does
            # exist in repo, resolution just could not pin it.
            if (_name_cap and strategy.startswith("name")
                    and len(wanted) > _name_cap):
                cov.unresolved += 1
                stats_counters["cap_dropped_emit"] += 1
                # Broken out per ref type because the cost is NOT uniform across
                # them, and the aggregate hides that. This site drops the real
                # edge of whatever type the ref is, so a CALLS drop is a genuine
                # recall loss — unlike the unknown_recv site below, whose edges
                # are REFERENCES and would be filtered by DROPPED_EDGE_TYPES at
                # write time regardless. READS/WRITES never appear here at all:
                # their strategies are same_scope/same_file_global, neither of
                # which starts with `name`.
                cap_dropped_by_type[ref.type] += 1
                return
            for c in wanted:
                out_edges.append(
                    make_edge(ref, c.id, _CONF_AMBIGUOUS, strategy=strategy)
                )
            cov.ambiguous += 1
        elif known_in_repo:
            cov.unresolved += 1   # the name exists in-repo but scope didn't pin it
        else:
            cov.external += 1     # nothing by that name here -> stdlib/3rd-party/builtin

    def _resolve_one_ref(ref: RawRef, cov: Coverage) -> None:
        """Resolve a single ref (the body of the old inline loop, extracted
        so the caller can wrap it in a try/except — one malformed/unexpected
        ref can no longer crash the entire resolve() pass; it's now isolated
        the same way _extract_one already isolates one bad file during
        extraction. `continue` in the original loop becomes `return` here;
        no matching/resolution logic itself changed."""
        # Dropped types cost nothing beyond the RawRef that already exists: no
        # candidate lookup, no Endpoint/Event/Policy node synthesis, no edge.
        # store.write_edges filters them too (it has to — the REFERENCES fallback
        # below is created while resolving a CALLS ref, not a REFERENCES one), but
        # returning here is what avoids the resolution work. See
        # schema.DROPPED_EDGE_TYPES.
        if ref.type in DROPPED_EDGE_TYPES:
            return
        if ref.type == "ANNOTATED_WITH":
            # @Override and friends: no Annotation node, no edge. See
            # schema.NOISE_ANNOTATIONS — real annotations still resolve, this only
            # skips compiler/linter directives, of which @Override is usually the
            # single highest-volume annotation in a Java repo.
            if ref.target_name in NOISE_ANNOTATIONS:
                cov.external += 1
                return
            aid = annotation_ids.get(ref.target_name)
            if aid is None:
                aid = make_id(repo, f"@{ref.target_name}", "annotation")
                annotation_ids[ref.target_name] = aid
                extra_nodes.append(Node(
                    id=aid, label="Annotation", name=ref.target_name,
                    fqn=f"@{ref.target_name}", repo=repo, kind="annotation",
                ))
            out_edges.append(make_edge(ref, aid, _CONF_EXTRACTED, strategy="annotation"))
            cov.resolved += 1
            return

        if ref.type in ("EMITS_EVENT", "CONSUMES_EVENT"):
            topic = _normalize_event_name(ref.target_name)
            if not topic:
                cov.unresolved += 1
                return
            eid = event_ids.get(topic)
            if eid is None:
                eid = make_id(repo, f"event:{topic}", "event")
                event_ids[topic] = eid
                extra_nodes.append(Node(
                    id=eid, label="Event", name=topic,
                    fqn=f"event://{topic}", repo=repo, kind="event",
                    display_name=ref.target_name,
                ))
            if ref.strategy_hint == "fuzzy_name":
                conf, strat = _CONF_AMBIGUOUS, "fuzzy_name"
            else:
                conf, strat = _CONF_EXTRACTED, "event_marker"
            out_edges.append(make_edge(ref, eid, conf, strategy=strat))
            cov.resolved += 1
            return

        if ref.type in ("REQUIRES_AUTH", "ENFORCES_POLICY"):
            pname = _normalize_policy_name(ref.target_name)
            if not pname:
                pname = "AUTH_REQUIRED" if ref.type == "REQUIRES_AUTH" else "POLICY"
            pid = policy_ids.get(pname)
            if pid is None:
                pid = make_id(repo, f"policy:{pname}", "policy")
                policy_ids[pname] = pid
                extra_nodes.append(Node(
                    id=pid, label="Policy", name=pname,
                    fqn=f"policy://{pname}", repo=repo, kind="policy",
                ))
            if ref.strategy_hint == "fuzzy_name":
                conf, strat = _CONF_AMBIGUOUS, "fuzzy_name"
            else:
                conf, strat = _CONF_EXTRACTED, "auth_marker"
            out_edges.append(make_edge(ref, pid, conf, strategy=strat))
            cov.resolved += 1
            return

        if ref.type == "CALLS_API":
            method = (ref.http_method or "GET").upper()
            route = normalize_route(ref.target_name)
            host = ref.recv  # carries the external host ('' for a relative URL)
            hits = match_endpoints(method, route)
            if hits:
                # An in-repo backend exposes this route (resolves cross-file).
                for ep in hits:
                    out_edges.append(
                        make_edge(ref, ep.id, _CONF_EXTRACTED, strategy="api_match")
                    )
                cov.resolved += 1
            else:
                # No in-repo handler: synthesize the target so the edge lands.
                # External host -> external Endpoint (shared id across repos);
                # relative path -> an unresolved in-repo Endpoint (a likely dead/
                # missing route worth surfacing).
                if host:
                    eid = endpoint_id("external", method, route, host)
                    erepo, conf, strat = "external", _CONF_EXTRACTED, "api_external"
                else:
                    eid = endpoint_id(repo, method, route)
                    erepo, conf, strat = repo, _CONF_INFERRED, "api_unresolved"
                if eid not in api_endpoint_ids:
                    api_endpoint_ids.add(eid)
                    extra_nodes.append(Node(
                        id=eid, label="Endpoint",
                        name=endpoint_display(method, route, host),
                        fqn=endpoint_fqn(method, route, host),
                        repo=erepo, kind="endpoint",
                        method=method, route=route, host=host,
                    ))
                out_edges.append(make_edge(ref, eid, conf, strategy=strat))
                cov.resolved += 1
            return

        # Cross-language file references, all three resolved the same way: by
        # BASENAME against File nodes, narrowed to the language the edge can
        # legitimately target.
        #
        #   RENDERS          Java method -> the .jsp it forwards to
        #   INCLUDES_SCRIPT  .jsp page   -> the .js it loads
        #   INCLUDES_PAGE    .jsp page   -> a <%@ include %>d page
        #
        # A basename is all the source carries — every one of these paths is
        # assembled from a constant prefix plus a literal filename, so the
        # directory is not statically available. The language filter is what keeps
        # that safe: `common.js` and `common.jsp` cannot be confused, and a
        # RENDERS ref can never land on a JS file.
        if ref.type in ("RENDERS", "INCLUDES_SCRIPT", "INCLUDES_PAGE"):
            if ref.kind_hint != "file":
                cov.external += 1
                return
            wanted_langs = _FILE_REF_LANGS.get(ref.type)
            candidates = files_by_basename.get(ref.target_name) or []
            if wanted_langs:
                candidates = [c for c in candidates if c.lang in wanted_langs]
            # Same-directory preference. Two pages named `index.jsp` in different
            # folders is routine in a webapp, and the one in the referrer's own
            # directory is overwhelmingly the intended target — this is the
            # file-level analogue of narrow_call's same_file step.
            strategy = "file_basename"
            if len(candidates) > 1 and ref.ref_file:
                ref_dir = ref.ref_file.replace("\\", "/").rpartition("/")[0]
                same_dir = [
                    c for c in candidates
                    if (c.file or "").replace("\\", "/").rpartition("/")[0] == ref_dir
                ]
                if same_dir:
                    candidates, strategy = same_dir, "file_same_dir"
            emit(
                ref,
                cov,
                candidates,
                _CONF_EXTRACTED,
                known_in_repo=bool(files_by_basename.get(ref.target_name)),
                strategy=strategy,
            )
            return

        # A JS call naming its Java handler class outright — the strongest
        # cross-language signal available, because no URL, convention or route
        # table sits in between. Exact FQN first (the AJAX-handler form spells the
        # whole package), then the bare simple name (the form-action form, whose
        # package lives in a JS constant this resolver cannot read).
        if ref.type == "HANDLED_BY":
            exact = classes_by_fqn.get(ref.target_name)
            if exact is not None:
                out_edges.append(make_edge(
                    ref, exact.id, _CONF_EXTRACTED, strategy="handler_fqn",
                ))
                cov.resolved += 1
                return
            tail = _tail_name(ref.target_name)
            emit(
                ref,
                cov,
                classes_by_name.get(tail) or [],
                _CONF_INFERRED,
                known_in_repo=bool(classes_by_name.get(tail)),
                strategy="handler_name",
            )
            return

        # ("CALLS", "PASSES") until PASSES was removed — no extractor ever emitted
        # a PASSES ref (verified), so that arm was unreachable.
        if ref.type == "CALLS":
            # This file's calls belong to a type-precise resolver — emit nothing.
            # ref_file is already repo-relative, matching attributed_files, so no
            # node lookup is needed. Counted as external so coverage totals stay
            # honest: from this resolver's point of view the target is not its
            # to find.
            if _skip_files and ref.ref_file in _skip_files:
                cov.external += 1
                return
            wanted, strategy = narrow_call(ref)
            # Receiver typed to something outside the repo — emit nothing at all.
            # This must short-circuit BOTH the REFERENCES demotion below and the
            # tail-name fallback further down: each of those would otherwise
            # re-materialize the exact fan-out narrow_call just declined to
            # produce, only relabelled as REFERENCES. `external` is the honest
            # bucket — the target genuinely is not in this codebase.
            if strategy == "external_receiver":
                cov.external += 1
                # The call is not lost, only its in-repo destination. Before the
                # external-receiver fix these fanned out into garbage; after it
                # they vanished entirely, taking with them the only record that
                # a function touches a database. Keep the fact.
                #
                # No return type is available here (recv_type is all the
                # extractor carries), so this catches DB work by owner type
                # only. The bytecode path, which has descriptors, additionally
                # catches acquires by return type — see external_api.
                kind = classify_call(ref.recv_type, ref.target_name)
                # Taint marking for the NON-BYTECODE path. The bytecode resolver
                # does this for Java with class files; this is the same fact for
                # everything else, and "everything else" is not an edge case:
                # .jsp is compiled by the servlet container at RUNTIME, so a JSP
                # page never has a class file and its sinks are invisible without
                # this — and on the measured repo 18 of 20 CALLS_EXTERNAL edges
                # came from .jsp files.
                #
                # Weaker than the bytecode path by construction: recv_type is a
                # SIMPLE name (java.py stores types via simple_type_name), so the
                # lookup goes through JAVA_BY_SIMPLE_NAME, which deliberately
                # excludes names that map to more than one catalogued owner. An
                # ambiguous simple name matches nothing rather than guessing.
                if taint_marks is not None and ref.src:
                    _thit = classify_taint(ref.recv_type, ref.target_name)
                    if _thit is not None:
                        _tentry, _targs = _thit
                        _m = taint_marks.get(ref.src)
                        if _m is None:
                            _m = {"cats": set(), "src": False, "sites": []}
                            taint_marks[ref.src] = _m
                        if _tentry.role == "source":
                            _m["src"] = True
                        elif _tentry.role == "sink":
                            _m["cats"].add(_tentry.category)
                        if ref.ref_line:
                            _m["sites"].append(
                                (ref.ref_line, _tentry.category, _tentry.role,
                                 _targs))
                if kind:
                    eid = external_id(ref.recv_type, ref.target_name)
                    if eid not in external_ids:
                        external_ids.add(eid)
                        extra_nodes.append(Node(
                            id=eid, label="External",
                            name=external_display(ref.recv_type, ref.target_name),
                            fqn=external_key(ref.recv_type, ref.target_name),
                            repo="external", kind=kind,
                        ))
                    out_edges.append(make_edge(
                        ref, eid, _CONF_EXTRACTED,
                        strategy="external_typed", edge_type="CALLS_EXTERNAL",
                    ))
                return
            # Precision guard: a call on an *unknown* receiver that matched only by
            # global name (no scope/file/import/receiver-type evidence) is not a
            # trustworthy CALLS — it likely targets an external object that merely
            # shares a method name. Demote it to a weak REFERENCES symbol-use edge
            # instead of asserting a precise call. Bare calls (no receiver) are
            # left untouched.
            if (
                ref.type == "CALLS"
                and wanted
                and strategy.startswith("name")
                and ref.recv
                and ref.recv not in ("self", "cls")
            ):
                cov.total -= 1  # this site is accounted under REFERENCES, not CALLS
                rcov = coverage["REFERENCES"]
                rcov.total += 1
                if len(wanted) == 1:
                    out_edges.append(make_edge(
                        ref, wanted[0].id, _CONF_INFERRED,
                        strategy=f"{strategy}+unknown_recv", edge_type="REFERENCES",
                    ))
                    rcov.resolved += 1
                # AMBIGUITY CAP, applied here too — this branch bypasses emit(),
                # so until now it was the one fan-out path with NO cap at all,
                # and it is by far the largest: HANDOFF measured 8,236,060 of
                # 8.38M REFERENCES arriving here (`name+arity+unknown_recv`). Every
                # one of those sites emits one edge PER same-named candidate, of
                # which at most one can be right, and the AMBIGUOUS bucket as a
                # whole measured 0.1% precision while contributing 1.0% recall
                # (HANDOFF 3.2). Not allocating them saves memory three times
                # over — in resolve, in derive's whole-graph passes, and in the
                # write — which is what makes the build fit a hard RAM cap.
                #
                # Reuses the same _name_cap dial and the same `name*` scoping as
                # emit(), so one setting governs both paths and the trusted tiers
                # (same_scope/same_file/imports/receiver_type) stay uncapped.
                elif _name_cap and len(wanted) > _name_cap:
                    rcov.unresolved += 1
                    stats_counters["cap_dropped_unknown_recv"] += 1
                else:
                    for c in wanted:
                        out_edges.append(make_edge(
                            ref, c.id, _CONF_AMBIGUOUS,
                            strategy=f"{strategy}+unknown_recv", edge_type="REFERENCES",
                        ))
                    rcov.ambiguous += 1
                return
            emit(
                ref,
                cov,
                wanted,
                _CONF_INFERRED,
                known_in_repo=ref.target_name in by_name,
                strategy=strategy,
            )
            if not wanted:
                # Fallback: keep recall via a weaker symbol-use edge when a
                # call target can't be resolved as a CALLS destination.
                fallback = _fallback_reference_candidates(ref.target_name, by_name)
                if fallback:
                    rcov = coverage["REFERENCES"]
                    rcov.total += 1
                    if len(fallback) == 1:
                        out_edges.append(
                            make_edge(
                                ref,
                                fallback[0].id,
                                _CONF_INFERRED,
                                strategy=f"{strategy or 'none'}+fallback_tail",
                                edge_type="REFERENCES",
                            )
                        )
                        rcov.resolved += 1
                    else:
                        for c in fallback:
                            out_edges.append(
                                make_edge(
                                    ref,
                                    c.id,
                                    _CONF_AMBIGUOUS,
                                    strategy=f"{strategy or 'none'}+fallback_tail",
                                    edge_type="REFERENCES",
                                )
                            )
                        rcov.ambiguous += 1
            return

        if ref.type in ("READS", "WRITES"):
            # self.<field> resolved to the enclosing class's field — scope-exact.
            cid = enclosing_class_id(ref.src)
            wanted = _members(fields_of_class, (cid, ref.target_name)) if cid else []
            strategy = "same_scope"
            if not wanted:
                # Not inside a class (or not a class field) — fall back to a
                # module-level global owned by the same file.
                src_node = nodes_by_id.get(ref.src)
                if src_node is not None and src_node.file:
                    wanted = _members(fields_of_file, (src_node.file, ref.target_name))
                    strategy = "same_file_global"
            emit(
                ref,
                cov,
                wanted,
                _CONF_EXTRACTED,
                known_in_repo=bool(wanted),
                strategy=strategy,
            )
            return

        if ref.type in ("EXTENDS", "IMPLEMENTS", "INSTANTIATES", "AUTOWIRED"):
            wanted, strategy = narrow_type(ref)
            emit(
                ref,
                cov,
                wanted,
                _CONF_EXTRACTED if strategy not in ("name", "none") else _CONF_INFERRED,
                known_in_repo=bool(classes_by_name.get(_tail_name(ref.target_name))),
                strategy=strategy,
            )
            return

        # Fallback for any ref type not covered by a specific branch above —
        # only reached if none of the earlier `if ref.type == ...` blocks
        # matched and returned (same as falling past every `continue` did in
        # the original inline loop).
        candidates = by_name.get(ref.target_name, [])
        # type-shaped refs should resolve to classes; call-shaped to functions
        if ref.kind_hint in ("type", "import"):
            wanted = [c for c in candidates if c.label == "Class"]
        elif ref.kind_hint == "call":
            wanted = [c for c in candidates if c.label == "Function"]
        else:
            wanted = candidates
        wanted = wanted or candidates
        emit(
            ref,
            cov,
            wanted,
            _CONF_INFERRED,
            known_in_repo=ref.target_name in by_name,
            strategy="kind_hint",
        )

    total_refs = len(refs)
    last_report = time.monotonic()
    last_checkpoint = time.monotonic()
    _ckpt_every = resolve_checkpoint_seconds()

    if on_progress:
        # Index-building above (and, if resumed, loading the prior resolve
        # checkpoint) has no progress signal of its own — this fires once,
        # right as the main loop is about to start, so the caller's stage
        # reporter has something to show for that whole window instead of a
        # static, unchanging message. resume_index (not 0) so a resumed
        # resolve reports accurately rather than appearing to restart.
        on_progress(resume_index, total_refs)

    def _save_checkpoint(next_index: int) -> None:
        save_resolve_checkpoint(checkpoint_root, repo, {
            "total_refs": total_refs,
            "next_index": next_index,
            "extra_nodes": extra_nodes,
            "annotation_ids": annotation_ids,
            "event_ids": event_ids,
            "policy_ids": policy_ids,
            "api_endpoint_ids": api_endpoint_ids,
            "out_edges": out_edges,
            "coverage": coverage,
        })
        _log.info("[resolve][repo=%s] checkpointed at ref %s/%s", repo, next_index, total_refs)

    # A+B Step 11: hand contiguous slices of out_edges (plus the extra_nodes
    # synthesized since the previous flush) to the sink, which persists them
    # and returns slim stand-ins that replace the slice in place — bounding
    # full-object edge memory to ~one batch. Slice replacement keeps the same
    # list object (emit/make_edge close over out_edges) and preserves order.
    _SINK_BATCH = 10_000
    flushed_edges = 0
    flushed_nodes = 0

    def _sink_flush() -> None:
        nonlocal flushed_edges, flushed_nodes
        if flushed_edges == len(out_edges) and flushed_nodes == len(extra_nodes):
            return
        out_edges[flushed_edges:] = edge_sink(
            extra_nodes[flushed_nodes:], out_edges[flushed_edges:],
        )
        flushed_edges = len(out_edges)
        flushed_nodes = len(extra_nodes)

    # Enumerate from the start (not refs[resume_index:]) so `refs` may be a
    # streaming, non-indexable source (checkpoint.RefStream) as well as a plain
    # list — already-processed refs are skipped below. For a list this is
    # identical iteration to before; for a resumed run it re-reads (cheaply
    # skips) the already-done prefix rather than slicing, which a stream can't do.
    # The lookup indices built above are read-only from here on, and on a large
    # repo they are millions of live container objects. CPython's cyclic GC
    # rescans every one of them on each gen-2 pass, repeatedly, for nothing —
    # they are all reachable and none can be collected while resolve runs.
    # freeze() moves everything currently alive into a permanent generation the
    # collector skips, so gen-2 passes walk only the churn this loop produces.
    #
    # Deliberately NOT gc.disable(): that would stop cycles created BY the loop
    # from ever being collected, an unbounded leak over millions of refs and
    # unacceptable on the memory-capped repos this targets. freeze() alone
    # cannot leak — it only reclassifies objects that are already reachable.
    #
    # unfreeze() in `finally` so the permanent generation does not outlive this
    # call: the indices become garbage once resolve returns, and leaving them
    # frozen would exempt them from collection for the rest of the process —
    # straight through the derive and write stages that follow.
    gc.freeze()
    try:
        for _i, ref in enumerate(refs, start=1):
            if _i <= resume_index:
                continue
            if (on_progress or cancel_check) and (_i % 50_000 == 0 or time.monotonic() - last_report >= 3.0):
                last_report = time.monotonic()
                if on_progress:
                    on_progress(_i, total_refs)
                if cancel_check and cancel_check():
                    raise IngestCancelled(f"resolve cancelled at ref {_i}/{total_refs}")
            if checkpoint_root and (_i % 200_000 == 0 or time.monotonic() - last_checkpoint >= _ckpt_every):
                last_checkpoint = time.monotonic()
                _save_checkpoint(_i)
            if edge_sink is not None and len(out_edges) - flushed_edges >= _SINK_BATCH:
                _sink_flush()
            cov = coverage[ref.type]
            cov.total += 1
            try:
                _resolve_one_ref(ref, cov)
            except Exception as exc:
                cov.external += 1
                _log.warning(
                    "[resolve][repo=%s] skipping ref (type=%s target=%s) after error: %s",
                    repo, ref.type, getattr(ref, "target_name", ""), exc,
                )

        if edge_sink is not None:
            _sink_flush()
    finally:
        gc.unfreeze()

    # ---- resolve summary -----------------------------------------------
    # resolve() previously returned `coverage` and logged NONE of it, so the only
    # way to see what a multi-million-ref pass actually did was to query Neo4j
    # afterwards. These three lines are the difference between "the run finished"
    # and "here is what it decided".
    for _rt, _c in sorted(coverage.items()):
        if not _c.total:
            continue
        _log.info(
            "[resolve][repo=%s] %-16s total=%s resolved=%s ambiguous=%s "
            "unresolved=%s external=%s (%.1f%% of in-repo targets pinned)",
            repo, _rt, _c.total, _c.resolved, _c.ambiguous, _c.unresolved,
            _c.external, _c.pct(),
        )

    # Top tiers only: a big repo produces a long tail of strategy variants and
    # dumping all of them buries the ones that matter.
    _top = sorted(strategy_counts.items(), key=lambda kv: -kv[1])[:15]
    _log.info(
        "[resolve][repo=%s] edges by strategy (top %s of %s): %s",
        repo, len(_top), len(strategy_counts),
        ", ".join(f"{k}={v}" for k, v in _top),
    )
    # The ancestor fallback's actual payoff, as opposed to the size of the index
    # it built. HANDOFF measured 8,236,060 refs landing in the
    # `name+arity+unknown_recv` bucket because a class's own methods were searched
    # but its inherited ones were not; this is how many of those now bind to a
    # real declaration instead.
    _inherited = sum(v for k, v in strategy_counts.items() if "_inherited" in k)
    _log.info(
        "[resolve][repo=%s] inheritance-aware resolution produced %s edge(s); "
        "ambiguity cap (max %s candidates) dropped %s bare-name site(s) and %s "
        "unknown-receiver site(s)",
        repo, _inherited, _name_cap,
        stats_counters["cap_dropped_emit"],
        stats_counters["cap_dropped_unknown_recv"],
    )
    # The unknown-receiver figure above is NOT a graph loss: that branch emits
    # REFERENCES, which store.write_edges filters via DROPPED_EDGE_TYPES on every
    # run. Only the bare-name figure costs real edges, so spell out where.
    if cap_dropped_by_type:
        _log.info(
            "[resolve][repo=%s] ambiguity cap: bare-name drops by ref type %s "
            "(these cost real edges); the %s unknown-receiver drop(s) cost none "
            "— those edges are REFERENCES, dropped at write regardless",
            repo,
            dict(sorted(cap_dropped_by_type.items(), key=lambda kv: -kv[1])),
            stats_counters["cap_dropped_unknown_recv"],
        )

    return extra_nodes, out_edges, coverage


def _route_segments(route: str) -> list[str]:
    return [s for s in route.split("/") if s]


def _tail_name(name: str) -> str:
    if not name:
        return ""
    # rfind+slice instead of split(".")[-1]: the split allocated a full list of
    # every dotted segment just to read the last one and throw the rest away.
    # Called ~493k times per resolve at a modest corpus size (and it scales with
    # ref count), so the per-call list was pure garbage-collector pressure.
    # Identical result — rfind returns -1 when there is no dot, and -1 + 1 == 0
    # slices the whole string, which is exactly what split would have yielded.
    return name[name.rfind(".") + 1:]


def _apply_arity(ref: RawRef, candidates: list[Node], base_strategy: str) -> tuple[list[Node], str]:
    if ref.call_arity < 0:
        return candidates, base_strategy
    narrowed = [c for c in candidates if c.param_count == ref.call_arity]
    if narrowed:
        return narrowed, f"{base_strategy}+arity"
    return candidates, base_strategy


def _import_qualified_hits(candidates: list[Node], tails: frozenset,
                            segs_cache: dict) -> list[Node]:
    """Candidates whose FQN contains some imported name's tail as a
    non-leading dotted segment (i.e. ``.{tail}.`` appears in the FQN, or the
    FQN ends with ``.{tail}``) — a deterministic namespace/prefix match.

    ``.{tail}.`` in fqn or fqn.endswith(f".{tail}") is exactly "tail equals
    some segment of fqn.split('.') other than the first" (the first segment
    can never have a preceding dot), so this is a set intersection per
    candidate rather than a scan over every import string.

    Both inputs are now precomputed by the caller instead of rebuilt here:

    ``tails`` was derived from the file's imports on EVERY call, so a file's
    import set was re-tailed once per ref in that file. It depends only on the
    file, so resolve() builds it once per file up front.

    ``segs_cache`` memoizes each candidate's fqn segments by node id. The
    previous code called ``(c.fqn or "").split(".")`` per candidate per call —
    measured at ~826k splits on a modest corpus, and it scales with
    (refs x pool size). Populated lazily rather than for every node up front,
    so its memory tracks the candidates actually examined (hot names get
    cached; the long tail of never-probed nodes costs nothing) — deliberate,
    given how tight the RAM budget is on the repos this runs against.
    """
    if not tails:
        return []
    hits: list[Node] = []
    for c in candidates:
        segs = segs_cache.get(c.id)
        if segs is None:
            parts = (c.fqn or "").split(".")
            segs = frozenset(parts[1:]) if len(parts) > 1 else frozenset()
            segs_cache[c.id] = segs
        if segs & tails:
            hits.append(c)
    return hits


def _fallback_reference_candidates(
    target_name: str,
    by_name: dict[str, list[Node]],
) -> list[Node]:
    """Deterministic weak fallback for unresolved call-like refs.

    If a dotted callee token doesn't resolve directly (e.g. pkg.mod.fn),
    attempt tail-name matching and emit REFERENCES instead of dropping signal.
    """
    tail = _tail_name(target_name)
    if not tail or tail == target_name:
        return []
    cands = by_name.get(tail, [])
    if not cands:
        return []
    fns = [c for c in cands if c.label == "Function"]
    return fns or cands


def _normalize_event_name(name: str) -> str:
    n = (name or "").strip().strip("\"'")
    if not n:
        return ""
    n = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", ".", n)
    n = n.lower()
    n = re.sub(r"[_\-/:]+|\s+", ".", n)
    n = re.sub(r"\.{2,}", ".", n)
    return n.strip(".")


def _normalize_policy_name(name: str) -> str:
    n = (name or "").strip().strip("\"'")
    if not n:
        return ""
    n = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", ".", n)
    n = n.lower()
    n = re.sub(r"[_\-/:]+|\s+", ".", n)
    n = re.sub(r"\.{2,}", ".", n)
    return n.strip(".")

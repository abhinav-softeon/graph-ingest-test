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
from .ids import make_id
from .models import Confidence, Edge, IngestCancelled, Node, Origin, RawRef
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Enum member `.value` goes through Enum's descriptor protocol on EVERY access —
# a Python-level __get__ call, not a plain attribute load. These are read once
# or twice per emitted edge, so at multi-million-edge scale they were measured
# at ~1.18M descriptor calls and ~13% of resolve's total time, purely to fetch
# constant strings. Bound once here; the values are identical, so output is
# unchanged.
_CONF_EXTRACTED = Confidence.EXTRACTED.value
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
):
    """Return (extra_nodes, edges, coverage_by_reftype).

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
    for ref in refs:
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
    for n in nodes:
        if n.label != "Endpoint":
            continue
        method, route = match_key(n.method, n.route)
        endpoints_by_key[(method, route)].append(n)
        if "*" in route:
            endpoint_patterns[method].append((_route_segments(route), n))

    def match_endpoints(method: str, route: str) -> list[Node]:
        exact = endpoints_by_key.get((method, route))
        if exact:
            return exact
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
        if ref.recv_type and ref.recv_type in classes_by_name:
            src_file = src_node.file if src_node else ""
            narrowed = _narrow_classes_for_recv(classes_by_name[ref.recv_type], src_file)
            hits: list[Node] = []
            for ccls in narrowed:
                hits.extend(_members(methods_of_class, (ccls.id, name)))
            if hits:
                return _apply_arity(ref, hits, "receiver_type_hint")

        # self/cls dispatch -> a method of the enclosing class.
        if ref.recv in ("self", "cls"):
            cid = enclosing_class_id(ref.src)
            if cid is not None:
                m = _members(methods_of_class, (cid, name))
                if m:
                    return _apply_arity(ref, m, "receiver_type")

        # Receiver is an in-repo class name -> that class's methods.
        if ref.recv and ref.recv not in ("self", "cls") and ref.recv in classes_by_name:
            src_file = src_node.file if src_node else ""
            narrowed = _narrow_classes_for_recv(classes_by_name[ref.recv], src_file)
            hits: list[Node] = []
            for ccls in narrowed:
                hits.extend(_members(methods_of_class, (ccls.id, name)))
            if hits:
                return _apply_arity(ref, hits, "receiver_type")

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

    _log.info(
        "[resolve][repo=%s] lookup indices built in %.3fs", repo, time.monotonic() - _t_index_start,
    )

    extra_nodes: list[Node] = []
    annotation_ids: dict[str, str] = {}
    event_ids: dict[str, str] = {}
    policy_ids: dict[str, str] = {}
    api_endpoint_ids: set[str] = set()
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
        if ref.type == "ANNOTATED_WITH":
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

        # ("CALLS", "PASSES") until PASSES was removed — no extractor ever emitted
        # a PASSES ref (verified), so that arm was unreachable.
        if ref.type == "CALLS":
            wanted, strategy = narrow_call(ref)
            # Receiver typed to something outside the repo — emit nothing at all.
            # This must short-circuit BOTH the REFERENCES demotion below and the
            # tail-name fallback further down: each of those would otherwise
            # re-materialize the exact fan-out narrow_call just declined to
            # produce, only relabelled as REFERENCES. `external` is the honest
            # bucket — the target genuinely is not in this codebase.
            if strategy == "external_receiver":
                cov.external += 1
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

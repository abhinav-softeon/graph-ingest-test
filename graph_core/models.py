"""Core data structures emitted by extractors and consumed by the resolver/store."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum


class IngestCancelled(RuntimeError):
    """Raised when a caller-supplied cancel_check() fires during index_repo.

    Must actually propagate (not be swallowed into a partial-but-"successful"
    result) so ensure_graph_indexed's except-handler runs: it needs to know
    this attempt never reached the Neo4j write phase, so it can clean up a
    first-time ingest's now-orphaned partial state instead of leaving it
    marked "error" forever, and — just as importantly — never advance the
    namespace's codebase_hash to "ready" for an incomplete graph."""


class Confidence(str, Enum):
    EXTRACTED = "EXTRACTED"   # observed directly in syntax / resolved precisely
    INFERRED = "INFERRED"     # resolved by heuristic (name match)
    AMBIGUOUS = "AMBIGUOUS"   # multiple/uncertain resolution


class Origin(str, Enum):
    """How a fact entered the graph — orthogonal to Confidence.

    EXTRACTED = read off the AST / a language index (tree-sitter, SCIP).
    DERIVED   = computed by later analysis over the graph (call-graph closure,
                communities, blast-radius materialization, …).
    """
    EXTRACTED = "EXTRACTED"
    DERIVED = "DERIVED"


def _clean(d: dict) -> dict:
    """Drop empty values so we never write meaningless props to Neo4j."""
    out = {}
    for k, v in d.items():
        if v in ("", 0, None, False):
            continue
        if isinstance(v, (list, tuple, dict)) and not v:
            continue
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


@dataclass(slots=True)
class Node:
    id: str
    label: str          # File | Module | Class | Function | Field | Annotation
    name: str
    fqn: str
    repo: str
    kind: str = ""      # class|interface|enum|record / method|constructor|function|lambda
    lang: str = ""
    file: str = ""
    package: str = ""   # owning package/namespace fqn (File: its package)
    # source range (1-based line, 0-based column — matches tree-sitter points)
    start_line: int = 0
    start_col: int = 0
    end_line: int = 0
    end_col: int = 0
    # structural metadata (Milestone 1)
    display_name: str = ""
    visibility: str = ""              # public|private|protected|package
    modifiers: list[str] = field(default_factory=list)
    is_static: bool = False
    is_abstract: bool = False
    is_async: bool = False
    return_type: str = ""
    param_count: int = 0
    param_names: list[str] = field(default_factory=list)   # input parameter names (ordered)
    param_types: list[str] = field(default_factory=list)   # declared types aligned to param_names ("" if untyped)
    signature: str = ""
    docstring: str = ""
    body_hash: str = ""
    # HTTP-API metadata (Endpoint nodes)
    method: str = ""                  # GET|POST|... (Endpoint)
    route: str = ""                   # normalized URL path (Endpoint)
    host: str = ""                    # external host, e.g. api.stripe.com ("" = in-repo)
    # static metrics (M5)
    loc: int = 0
    cyclomatic: int = 0
    branch_count: int = 0
    loop_count: int = 0
    # derived architecture metadata
    component_role: str = ""          # controller|service|repository|entity|config|util|...
    role_source: str = ""             # annotation|name_suffix|package|fallback
    role_confidence: str = ""         # HIGH|MEDIUM|LOW
    module_id: str = ""               # owning Module node id (derived)
    # Field-node-only metadata
    scope: str = ""                   # Field: class|module — where the variable lives
    is_lock: bool = False             # Field: True if assigned a Lock/RLock/Semaphore/Condition
    # Taint marking (Function nodes). Set from the vulnerability catalog during
    # the bytecode pass, BEFORE the pre-resolve write — so they persist without
    # needing a place in SlimNode, since nothing post-resolve reads them back.
    #
    # taint_categories/taint_source are the cheap function-level facts a
    # reachability pass seeds from (analysis/reach.py already marks
    # `reaches_sink` this way off Pass A's LLM summary; these make the same seed
    # deterministic).
    #
    # NAMED taint_categories, NOT sink_kinds, and the distinction is load-bearing:
    # reach.py already writes `f.sink_kinds` at ANALYSIS time using the External
    # kind vocabulary (db_execute / exec / file_write). These carry CWE
    # CATEGORIES (CWE-89/sql-injection). Two vocabularies in one property would
    # have silently collided, and reach.py runs later, so it would have
    # overwritten every value set here with no error anywhere.
    #
    # taint_sites carries the part a boolean cannot: WHICH line, WHICH argument.
    # Without it a finding can only point at the function declaration, and the
    # dedup key for "same defect reached by five paths" has nothing to key on —
    # that key is (function, line), which is exactly a call site.
    taint_categories: list[str] = field(default_factory=list)
    taint_source: bool = False
    taint_sanitizer: bool = False
    taint_sites: str = ""            # compact JSON, "" when the function has none
    # provenance
    extractor: str = ""              # who produced this node (tree-sitter)
    confidence: str = Confidence.EXTRACTED.value

    def props(self) -> dict:
        """Property map written to Neo4j (everything except id, set via MERGE).

        Deliberately narrower than the dataclass. These fields are still
        computed and used DURING the build but are no longer persisted, because
        a sweep of every consumer in the sail monorepo found nothing reading
        them back (item #11):

          package        -- drives _build_package_tree here; the resulting
                            Package nodes + CONTAINS edges are the queryable form
          start_col/end_col, display_name, param_types, host
          loc/cyclomatic/branch_count/loop_count  -- metrics, unread
          role_source    -- the role pass itself went in item #3
          extractor      -- provenance, unread

        Dropping them shrinks every node row on the write path (the expensive
        tail), not the in-RAM objects — most are already blanked in RAM by the
        pre-resolve write. Booleans like is_abstract/is_static/is_async are KEPT:
        _clean drops False, so only the true ones cost anything, and they carry
        real semantics. Decorators/annotations are unaffected — they are
        ANNOTATED_WITH edges to Annotation nodes, not a node property."""
        return _clean({
            "name": self.name,
            "fqn": self.fqn,
            "repo": self.repo,
            "kind": self.kind,
            "lang": self.lang,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "visibility": self.visibility,
            "modifiers": self.modifiers,
            "is_static": self.is_static,
            "is_abstract": self.is_abstract,
            "is_async": self.is_async,
            "return_type": self.return_type,
            "param_count": self.param_count,
            "param_names": self.param_names,
            "signature": self.signature,
            "docstring": self.docstring,
            "body_hash": self.body_hash,
            "method": self.method,
            "route": self.route,
            "component_role": self.component_role,
            "role_confidence": self.role_confidence,
            "module_id": self.module_id,
            "scope": self.scope,
            "is_lock": self.is_lock,
            "taint_categories": self.taint_categories,
            "taint_source": self.taint_source,
            "taint_sanitizer": self.taint_sanitizer,
            "taint_sites": self.taint_sites,
            "confidence": self.confidence,
        })

    def to_slim(self) -> "SlimNode":
        """Project to a SlimNode carrying only the resolver-relevant fields.
        The full node's bulky payload is dropped here (and, under streaming
        ingest, is persisted to Neo4j instead of held in RAM)."""
        return SlimNode(
            id=self.id, label=self.label, name=self.name, fqn=self.fqn,
            kind=self.kind, lang=self.lang, file=self.file, package=self.package,
            scope=self.scope, param_count=self.param_count,
            method=self.method, route=self.route,
            repo=self.repo, start_line=self.start_line,
            start_col=self.start_col, end_line=self.end_line,
        )


# Every field read off a node from the pre-resolve write onward — by the
# resolver's matching logic AND by the post-resolve consumers (_derive_overrides,
# _build_package_tree, _derive_sql_links, validate_graph, scip_resolver). This is
# the ONLY set a slim projection must carry to keep output byte-identical;
# enforced by test_slim_node.py's guardrail, which AST-scans all of them. Kept as
# a module constant so the projection, the SlimNode record, and the guardrail all
# derive from one source of truth.
#
# The last four were added when `all_nodes` itself became slim (item #14): they
# are not used for resolution, but _derive_sql_links (repo, start_line, end_line),
# validate_graph (start_line, end_line) and scip_resolver (start_col) read them.
SLIM_NODE_FIELDS: tuple[str, ...] = (
    "id", "label", "name", "fqn", "kind", "lang",
    "file", "package", "scope", "param_count", "method", "route",
    "repo", "start_line", "start_col", "end_line",
)


@dataclass(slots=True, frozen=True)
class SlimNode:
    """Immutable, memory-light stand-in for a Node during resolution.

    Carries only SLIM_NODE_FIELDS — the fields resolver.py actually matches on —
    dropping the bulky payload (docstring, signature, param_types, …)
    that dominates a full Node's footprint but is never read while resolving.
    Duck-types as a Node for the resolver: it exposes the same attribute names,
    so resolve() accepts a list of these with no code change to its matching
    logic. Bulky data lives in Neo4j and is never loaded into the slim index."""
    id: str
    label: str
    name: str
    fqn: str
    kind: str = ""
    lang: str = ""
    file: str = ""
    package: str = ""
    scope: str = ""
    param_count: int = 0
    method: str = ""
    route: str = ""
    repo: str = ""
    start_line: int = 0
    start_col: int = 0
    end_line: int = 0


@dataclass(slots=True)
class Edge:
    type: str
    src: str            # source node id (resolved)
    dst: str            # destination node id (resolved)
    confidence: str = Confidence.EXTRACTED.value
    # provenance (Milestone 1)
    origin: str = Origin.EXTRACTED.value
    extractor: str = ""              # tree-sitter | scip-python | heuristic
    evidence_file: str = ""          # where the evidence for this edge lives
    evidence_line: int = 0           # 1-based
    evidence_col: int = 0            # 0-based
    strategy: str = ""              # resolver strategy used for destination selection
    # Carries payload only on CALLS edges; every other edge type leaves it
    # unused. Defaulting to None instead of an empty list saves a list object per
    # edge — on a multi-million-edge graph that is GBs of otherwise-wasted
    # empty-list overhead held through resolve→derive→write. props()'s _clean
    # drops empty AND None identically, so the Neo4j write (and the fingerprint)
    # are unchanged — see TIER3_MEMORY_PLAN.md Phase 4.
    #
    # Four sibling fields lived here — flow_from_param/flow_to_param/flow_lines/
    # const_args, the DFG parallel arrays on PASSES edges. Removed with PASSES
    # itself (zero consumers across the sail monorepo).

    def props(self) -> dict:
        return _clean({
            "confidence": self.confidence,
            "origin": self.origin,
            "evidence_line": self.evidence_line,
            "strategy": self.strategy,
        })

    def to_slim(self) -> "SlimEdge":
        """Project to a SlimEdge carrying only the fields any post-resolve
        consumer ever reads off an *existing* edge (see SLIM_EDGE_FIELDS).
        Used once the full object is durable in Neo4j (TIER3_MEMORY_PLAN.md
        §11) — the write-only provenance fields no longer need to be held in
        RAM for the SCIP-filter/derive tail."""
        # Interned: an edge's src/dst are node-id strings that are EQUAL to, but
        # separate objects from, the ids held on the nodes themselves (refs are
        # deserialized from checkpoint bundles independently of nodes), and
        # evidence_file repeats one relpath across every edge in a file. At
        # ~150 bytes per id string and millions of edges that duplication alone
        # measured ~2.4GB — interning collapses each distinct value to a single
        # shared object. Values are unchanged (intern returns an equal string),
        # so output is identical; the cost is one hash+lookup per field at
        # projection time. `type` comes from a tiny closed set.
        return SlimEdge(
            type=sys.intern(self.type), src=sys.intern(self.src), dst=sys.intern(self.dst),
        )


# Fields read off an *existing* Edge anywhere from resolve onward (verified by
# AST-scanning pipeline.py's derive passes and resolver.py for
# e.<attr>/ce.<attr>/support.<attr> reads; enforced by test_slim_edge.py's
# guardrail). origin/extractor/strategy are only ever set on NEWLY
# constructed edges, never read back off an existing one — and by the time a
# SlimEdge replaces the full object (after its Neo4j write), they're already
# durable there. Notably evidence_file/evidence_line/evidence_col ARE needed
# (by _synthesize_polymorphic_calls) despite not being part of the golden
# edge-set hash in §7 — a naive (type, src, dst)-only projection would have
# silently dropped them without the regression oracle noticing.
# `confidence` was carried here until its last reader — the DFG pass's
# calls_conf lookup — went away with PASSES (items #9/#10). Nothing reads it off
# an *existing* edge any more, so it is no longer projected.
SLIM_EDGE_FIELDS: tuple[str, ...] = (
    "type", "src", "dst",
)


@dataclass(slots=True, frozen=True)
class SlimEdge:
    """Immutable, memory-light stand-in for an already-persisted Edge.

    Duck-types as an Edge for every post-resolve consumer (SCIP CALLS filter,
    the derive passes, validate_graph) — same
    attribute names, so none of their matching/aggregation logic changes.
    Drops the write-only provenance fields (origin/extractor/strategy) and
    the write-only provenance fields."""
    type: str
    src: str
    dst: str


@dataclass(slots=True)
class RawRef:
    """An edge whose destination is only known by name; resolved later."""
    type: str
    src: str            # source node id (already resolved)
    target_name: str    # symbol name to resolve against the repo symbol index
    kind_hint: str = "" # 'call' | 'type' | 'import' | 'annotation'
    recv: str = ""      # call receiver tail: 'self'/'cls', a module/class/var name, or '' for a bare call
    recv_type: str = "" # inferred receiver class/type name when statically available
    import_fqn: str = "" # fully-qualified import path when available
    http_method: str = "" # HTTP verb for a CALLS_API ref (GET|POST|...); recv carries the host
    # location of the reference site (for edge provenance)
    ref_file: str = ""
    ref_line: int = 0   # 1-based
    ref_col: int = 0    # 0-based
    call_arity: int = -1
    strategy_hint: str = ""  # "fuzzy_name" when the ref came from a substring/loose heuristic

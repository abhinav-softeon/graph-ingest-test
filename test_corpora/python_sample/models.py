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
    fan_in: int = 0
    fan_out: int = 0
    recursive: bool = False
    # derived architecture metadata
    component_role: str = ""          # controller|service|repository|entity|config|util|...
    role_source: str = ""             # annotation|name_suffix|package|fallback
    role_confidence: str = ""         # HIGH|MEDIUM|LOW
    module_id: str = ""               # owning Module node id (derived)
    # Field-node-only metadata
    scope: str = ""                   # Field: class|module — where the variable lives
    is_lock: bool = False             # Field: True if assigned a Lock/RLock/Semaphore/Condition
    # DFG summary (computed by dataflow.py at index time)
    dfg_json: str = ""                                    # serialized DfgSummary
    dfg_returns_from_params: list[int] = field(default_factory=list)
    dfg_hash: str = ""                                    # body_hash at summary time
    # provenance
    extractor: str = ""              # who produced this node (tree-sitter)
    confidence: str = Confidence.EXTRACTED.value

    def props(self) -> dict:
        """Property map written to Neo4j (everything except id, set via MERGE)."""
        return _clean({
            "name": self.name,
            "fqn": self.fqn,
            "repo": self.repo,
            "kind": self.kind,
            "lang": self.lang,
            "file": self.file,
            "package": self.package,
            "start_line": self.start_line,
            "start_col": self.start_col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "display_name": self.display_name,
            "visibility": self.visibility,
            "modifiers": self.modifiers,
            "is_static": self.is_static,
            "is_abstract": self.is_abstract,
            "is_async": self.is_async,
            "return_type": self.return_type,
            "param_count": self.param_count,
            "param_names": self.param_names,
            "param_types": self.param_types,
            "signature": self.signature,
            "docstring": self.docstring,
            "body_hash": self.body_hash,
            "method": self.method,
            "route": self.route,
            "host": self.host,
            "loc": self.loc,
            "cyclomatic": self.cyclomatic,
            "branch_count": self.branch_count,
            "loop_count": self.loop_count,
            "fan_in": self.fan_in,
            "fan_out": self.fan_out,
            "recursive": self.recursive,
            "component_role": self.component_role,
            "role_source": self.role_source,
            "role_confidence": self.role_confidence,
            "module_id": self.module_id,
            "scope": self.scope,
            "is_lock": self.is_lock,
            "dfg_json": self.dfg_json,
            "dfg_returns_from_params": self.dfg_returns_from_params,
            "dfg_hash": self.dfg_hash,
            "extractor": self.extractor,
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
        )


# Fields the resolver's matching logic reads off a node — the ONLY fields a slim
# projection must carry to produce byte-identical resolution (verified in
# TIER3_MEMORY_PLAN.md §4 and enforced by test_slim_node.py's guardrail). Kept as
# a module constant so the projection, the SlimNode record, and the guardrail
# test all derive from one source of truth.
SLIM_NODE_FIELDS: tuple[str, ...] = (
    "id", "label", "name", "fqn", "kind", "lang",
    "file", "package", "scope", "param_count", "method", "route",
)


@dataclass(slots=True, frozen=True)
class SlimNode:
    """Immutable, memory-light stand-in for a Node during resolution.

    Carries only SLIM_NODE_FIELDS — the fields resolver.py actually matches on —
    dropping the bulky payload (docstring, signature, dfg_json, param_types, …)
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
    # These 5 fields carry payload ONLY on PASSES edges (flow_*) and CALLS/PASSES
    # (arg_names); every other edge type leaves them unused. Defaulting to None
    # instead of an empty list saves ~5 list objects (~280 bytes) per edge — on a
    # multi-million-edge graph that is ~2GB of otherwise-wasted empty-list
    # overhead held through resolve→derive→write. props()'s _clean drops empty
    # AND None identically, so the Neo4j write (and the fingerprint) are
    # unchanged; and no ingest code iterates these (analysis reads them back from
    # Neo4j, never from these in-memory objects) — see TIER3_MEMORY_PLAN.md Phase 4.
    arg_names: list[str] | None = None       # lightweight arg-flow payload (PASSES/CALLS)
    # DFG parallel arrays on PASSES edges (index-aligned per recorded ArgFlow)
    flow_from_param: list[int] | None = None  # caller param index (-1 = no param origin)
    flow_to_param: list[int] | None = None    # callee param index (-1 = unmappable)
    flow_lines: list[int] | None = None       # call-site line per entry
    const_args: list[int] | None = None       # callee param positions that receive only literals

    def props(self) -> dict:
        return _clean({
            "confidence": self.confidence,
            "origin": self.origin,
            "extractor": self.extractor,
            "evidence_file": self.evidence_file,
            "evidence_line": self.evidence_line,
            "evidence_col": self.evidence_col,
            "strategy": self.strategy,
            "arg_names": self.arg_names,
            "flow_from_param": self.flow_from_param,
            "flow_to_param": self.flow_to_param,
            "flow_lines": self.flow_lines,
            "const_args": self.const_args,
        })

    def to_slim(self) -> "SlimEdge":
        """Project to a SlimEdge carrying only the fields any post-resolve
        consumer ever reads off an *existing* edge (see SLIM_EDGE_FIELDS).
        Used once the full object is durable in Neo4j (TIER3_MEMORY_PLAN.md
        §11) — the provenance fields and PASSES/CALLS payload arrays no longer
        need to be held in RAM for the SCIP-filter/dataflow/derive tail."""
        # Interned: an edge's src/dst are node-id strings that are EQUAL to, but
        # separate objects from, the ids held on the nodes themselves (refs are
        # deserialized from checkpoint bundles independently of nodes), and
        # evidence_file repeats one relpath across every edge in a file. At
        # ~150 bytes per id string and millions of edges that duplication alone
        # measured ~2.4GB — interning collapses each distinct value to a single
        # shared object. Values are unchanged (intern returns an equal string),
        # so output is identical; the cost is one hash+lookup per field at
        # projection time. type/confidence come from a tiny closed set.
        return SlimEdge(
            type=sys.intern(self.type), src=sys.intern(self.src), dst=sys.intern(self.dst),
            evidence_file=sys.intern(self.evidence_file), evidence_line=self.evidence_line,
            evidence_col=self.evidence_col, confidence=sys.intern(self.confidence),
        )


# Fields read off an *existing* Edge anywhere from resolve onward (verified by
# AST-scanning pipeline.py's derive passes, resolver.py, and dataflow.py for
# e.<attr>/ce.<attr>/support.<attr> reads; enforced by test_slim_edge.py's
# guardrail). origin/extractor/strategy/arg_names/flow_*/const_args are only
# ever set on NEWLY constructed edges, never read back off an existing one —
# and by the time a SlimEdge replaces the full object (after its Neo4j write),
# they're already durable there. Notably evidence_file/evidence_line/
# evidence_col ARE needed (by _synthesize_polymorphic_calls and
# _derive_module_ownership_and_uses) and `confidence` IS needed (dataflow.py's
# calls_conf lookup for PASSES edges) despite none of them being part of the
# golden edge-set hash in §7 — a naive (type, src, dst)-only projection would
# have silently dropped them without the regression oracle noticing.
SLIM_EDGE_FIELDS: tuple[str, ...] = (
    "type", "src", "dst", "evidence_file", "evidence_line", "evidence_col", "confidence",
)


@dataclass(slots=True, frozen=True)
class SlimEdge:
    """Immutable, memory-light stand-in for an already-persisted Edge.

    Duck-types as an Edge for every post-resolve consumer (SCIP CALLS filter,
    dataflow's CALLS index, the derive passes, validate_graph) — same
    attribute names, so none of their matching/aggregation logic changes.
    Drops the write-only provenance fields (origin/extractor/strategy) and
    the PASSES/CALLS payload arrays (arg_names/flow_*/const_args) that are
    never read back."""
    type: str
    src: str
    dst: str
    evidence_file: str = ""
    evidence_line: int = 0
    evidence_col: int = 0
    confidence: str = ""


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
    arg_names: list[str] = field(default_factory=list)  # optional arg names for PASSES
    strategy_hint: str = ""  # "fuzzy_name" when the ref came from a substring/loose heuristic

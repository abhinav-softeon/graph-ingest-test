"""Allowlists for node labels and edge types.

Used to validate any label/relationship-type that gets interpolated into a
Cypher string (Cypher cannot parametrize labels/rel-types), preventing
injection. Keep in sync with ../docs/ARCHITECTURE.md section 5.
"""
from __future__ import annotations

# Every node also carries the shared label CodeNode (single id index).
SHARED_LABEL = "CodeNode"

NODE_LABELS = {
    "Repository",   # the indexed repo root (one per repo)
    "Package",      # a package/namespace (Java package decl, Python dir path)
    "File",
    "Module",
    "Class",
    "Function",
    "Field",
    "Annotation",
    "Endpoint",     # an HTTP endpoint (method + route); in-repo or external
    "Event",        # an event/topic/queue semantic node
    "Policy",       # auth/policy contract node (role/scope/policy marker)
    "Table",        # a SQL table (CREATE TABLE); columns stored as JSON property
    # A call target OUTSIDE this repo, kept as a fact rather than discarded.
    # Keyed by owner type + method (e.g. `java.sql.Connection#close`) and
    # carrying a `kind` (db_acquire/db_execute/db_release/...). Has no file or
    # source position by construction — it is not code we hold. validate_graph
    # already guards its position checks on truthiness, so that is safe.
    "External",
}

EDGE_TYPES = {
    "CONTAINS",
    "BELONGS_TO",  # node -> Module ownership relation
    "IMPORTS",
    "CALLS",
    "INSTANTIATES",
    "EXTENDS",
    "IMPLEMENTS",
    "ANNOTATED_WITH",
    # Milestone 2 — type system
    "RETURNS",      # Function -> Class (declared return type)
    "OF_TYPE",      # Field -> Class (declared type)
    "HAS_TYPE",     # Function -> Class (a parameter's type)
    "HAS_GENERIC",  # carrier -> Class (a generic type argument, e.g. List[User])
    # Milestone 4 — program relationships
    "OVERRIDES",    # Function -> Function (overrides/implements a base method) [SCIP]
    "READS",        # Function -> Field (reads instance/class state)
    "WRITES",       # Function -> Field (mutates instance/class state)
    "THROWS",       # Function -> Class (raises an exception type)
    "CATCHES",      # Function -> Class (catches an exception type)
    # HTTP-API layer
    "EXPOSES",      # Function -> Endpoint (backend handler serves this route)
    "CALLS_API",    # Function -> Endpoint (outbound HTTP call to this route)
    # Flexible dependency layer (additive, lower-trust by default)
    "REFERENCES",   # generic symbol use when stricter typing is unavailable
    "USES",         # higher-level/component dependency
    # Function -> External. A call whose target is a library/JDK type, so no
    # in-repo Function can ever be the destination. Previously such calls were
    # either fanned out across every same-named method (100% false) or, after
    # the external-receiver fix, dropped entirely — losing the fact that the
    # function touches a database/connection at all.
    "CALLS_EXTERNAL",
    # "PASSES" (argument/data propagation hint) was removed — written by the
    # DFG pass, read by nothing. The pass itself is gone too (item #10).
    "AUTOWIRED",    # dependency-injection wiring relation
    "RE_EXPORTS",   # symbol forwarding/export indirection (mainly JS/TS ecosystems)
    # Event/auth layer
    "EMITS_EVENT",      # Function -> Event (publishes to topic/queue)
    "CONSUMES_EVENT",   # Function -> Event (subscribes/listens to topic/queue)
    "REQUIRES_AUTH",    # Function/Class -> Policy (auth requirement)
    "ENFORCES_POLICY",  # Function/Class -> Policy (authorization rule)
}


# Emitted by the extractors but NOT resolved and NOT written. Security/correctness
# analysis reads function source anyway, so an edge that only restates what is
# visible in that source is pure write cost.
#
# The rule used to pick these: keep an edge if it either routes the analysis to
# code worth reading, or carries a fact the reader CANNOT see in the one function
# it has open. Dropped here are the ones that fail both tests — a `throws` clause,
# a parameter type and a generic argument are all sitting in the signature the
# model is about to read.
#
# KEPT deliberately, and why:
#   CALLS / CALLS_EXTERNAL  the paths, and the sinks they end at
#   READS / WRITES          taint crossing methods with NO call between them
#   OF_TYPE                 a field's declared type (Connection vs InputStream),
#                           declared elsewhere in the class than the reader looks
#   OVERRIDES / EXTENDS / IMPLEMENTS / INSTANTIATES   which implementation runs
#   CONTAINS                the scope chain; resolve and derive both read it
#   ANNOTATED_WITH / EXPOSES   endpoint discovery = the taint SOURCES
#
# These stay in EDGE_TYPES on purpose. assert_edge guards Cypher interpolation,
# so delisting a type that something still emits turns a silent no-op into a
# write-time crash. Filtering instead of delisting is why this is reversible:
# empty the set and the edges come back with no other change.
#
# Filtered at two choke points rather than at ~20 emission sites across six
# extractors: store.write_edges (catches everything, including edges created
# inside resolve like the REFERENCES fallback) and resolver._resolve_one_ref
# (skips the resolution work too, not just the write). None of these types are in
# pipeline's _RETAINED_EDGE_TYPES, so no derive pass can be reading them.
#
# Caveat: the in-process edge_stats/coverage tallies still count what was
# produced, so they over-report against store.counts(). db_counts is the truth.
DROPPED_EDGE_TYPES = frozenset({
    "RETURNS",          # in the signature being read
    "HAS_TYPE",         # ditto
    "HAS_GENERIC",      # ditto
    "THROWS",           # in the throws clause / throw statement being read
    "CATCHES",          # in the catch block being read
    "REFERENCES",       # "related somehow" — the resolver's lowest-trust fallback
    "USES",             # component-level aggregation, too coarse to act on
    "BELONGS_TO",       # Module ownership; modules are not part of this analysis
    "RE_EXPORTS",       # JS/TS export forwarding
    "CALLS_API",        # outbound HTTP
    "EMITS_EVENT",      # event/queue layer, unused here
    "CONSUMES_EVENT",
    "REQUIRES_AUTH",    # policy layer, unused here
    "ENFORCES_POLICY",
    # IMPORTS: one edge per import statement, and NOTHING reads them back.
    # resolve() builds its import maps from the RawRef list directly (see the
    # wildcard_pkgs_by_file / import_fqns_by_file loop near the top of resolve),
    # which happens before and independently of edge emission — verified by
    # running the same cross-package call resolution with and without this type
    # dropped and diffing the result: identical, still `receiver_type_hint+arity`.
    # So the imports_qualified/imports strategies are unaffected. For LLM context
    # the import lines come from the file source, not from graph edges.
    "IMPORTS",
})

# Annotations that are pure compiler/tooling directives. ANNOTATED_WITH is KEPT
# (it is how endpoints are discovered — @WebService/@WebMethod/@RequestMapping —
# and endpoints are the taint SOURCES), but @Override alone is typically the
# largest single annotation in a Java codebase and carries no signal any analysis
# would act on. Deliberately conservative: only directives that tell a compiler
# or linter something, never anything describing behavior, lifecycle or security.
NOISE_ANNOTATIONS = frozenset({
    "Override",
    "SuppressWarnings",
    "SafeVarargs",
    "FunctionalInterface",
    "Generated",
    "SuppressFBWarnings",
})


def assert_label(label: str) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"unknown node label: {label!r}")
    return label


def assert_edge(rtype: str) -> str:
    if rtype not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {rtype!r}")
    return rtype

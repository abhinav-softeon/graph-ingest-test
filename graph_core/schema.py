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
    "DEFINES",      # semantic ownership/provenance (file/class defines symbol)
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


def assert_label(label: str) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"unknown node label: {label!r}")
    return label


def assert_edge(rtype: str) -> str:
    if rtype not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {rtype!r}")
    return rtype

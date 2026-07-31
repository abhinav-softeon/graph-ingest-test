"""Map bytecode members onto the graph nodes tree-sitter already extracted.

Bytecode resolves edges; tree-sitter owns nodes (IMPLEMENTATION_PLAN.md D1).
This module is the join between them, and it is a LOOKUP, never a computation —
D4 in the plan, forced by how java.py builds identity:

    fqn = f"{class_fqn}#{name}"                        # no parameters
    mid = make_id(repo, f"{fqn}{params}", "method")    # params = RAW SOURCE TEXT

`params` is the literal source slice, `(long id, String name)`, parameter names
and whitespace included. Bytecode has only the erased descriptor
`(JLjava/lang/String;)`. The node id is therefore *not reconstructible* from a
class file, so the only sound approach is to index the extracted nodes and look
matches up.

Three name systems have to be reconciled, and each has a trap:

  binary name    com.acme.Outer$Inner   nested classes joined with '$'
  source fqn     com.acme.Outer.Inner   java.py:191 joins with '.'
  descriptor     Ljava/util/List;       erased, no generics
                 vs node param_types    'List' — generics AND arrays stripped

WHEN IN DOUBT, DON'T. An ambiguous match is reported and dropped, never
guessed. A wrong edge asserted with EXTRACTED confidence is worse than no edge:
the whole point of this work is that consumers can trust `strategy='bytecode'`
enough to walk multi-hop paths, and precision compounds along a path.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .classfile import ClassInfo, MethodInfo, descriptor_param_types, type_name


def binary_to_source_fqn(binary_name: str) -> str:
    """Binary class name to the fqn java.py would have produced.

    ``com.acme.Outer$Inner`` -> ``com.acme.Outer.Inner``. Returns '' for
    anonymous classes (``Outer$1``), which have no tree-sitter node at all —
    callers must route those through node synthesis instead of lookup.

    '$' is legal in Java identifiers, so this can in principle mis-split a
    class genuinely named ``Foo$Bar``. That is accepted: such names are
    vanishingly rare, and the failure mode is a missed match (reported as
    unmatched) rather than a wrong edge.
    """
    if "$" not in binary_name:
        return binary_name
    head, _, tail = binary_name.rpartition("$")
    if tail.isdigit():
        return ""            # anonymous — no source-level declaration exists
    return binary_name.replace("$", ".")


def normalize_type(raw: str) -> str:
    """Reduce a type to the form java.py's ``param_types`` stores.

    Both sides must land on the same string, and java.py's simple_type_name
    strips generics, array brackets and package qualifiers. Varargs keep a
    trailing '...' on the node side only — bytecode compiles varargs to a plain
    array — so that is stripped here too.
    """
    raw = raw.split("<", 1)[0]
    raw = raw.replace("[]", "").replace("...", "").strip()
    # Inner types arrive as com.acme.Outer$Inner (bytecode) or Outer.Inner
    # (source); both reduce to the innermost simple name.
    for sep in (".", "$"):
        if sep in raw:
            raw = raw.rsplit(sep, 1)[-1]
    return raw


def descriptor_simple_types(descriptor: str) -> list[str]:
    """Parameter types of a method descriptor, normalized for comparison."""
    return [normalize_type(type_name(d)) for d in descriptor_param_types(descriptor)]


def method_lookup_name(source_fqn: str, method_name: str) -> str:
    """Bytecode method name to the name java.py gave the node.

    Constructors are ``<init>`` in bytecode; java.py names them after their
    class (the ``is_ctor`` branch). ``<clinit>`` has no source-level
    declaration and is left as-is for the synthesis path to handle.
    """
    if method_name == "<init>":
        return source_fqn.rsplit(".", 1)[-1]
    return method_name


@dataclass
class MatchStats:
    """Where every bytecode member went. Ambiguity is tracked, not hidden:
    a rising ambiguous count means the tiebreak is failing and precision is
    silently degrading."""
    matched_methods: int = 0
    matched_fields: int = 0
    unmatched_class: int = 0
    unmatched_method: int = 0
    unmatched_field: int = 0
    # javac emits a no-arg <init> for any class that declares no constructor.
    # It is real bytecode with no source declaration, and javac does NOT mark it
    # ACC_SYNTHETIC, so it would otherwise inflate unmatched_method on every
    # such class and mask genuine matching failures.
    implicit_default_ctor: int = 0
    ambiguous_method: int = 0
    anonymous_class: int = 0      # no source declaration — synthesis territory
    lambda_body: int = 0
    class_initializer: int = 0
    skipped_synthetic: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class NodeIndex:
    """Lookup over extracted nodes, keyed the way bytecode can actually ask.

    Built once per run over the FULL Node objects, which matters: this runs
    before pipeline.py's slim projection, so ``param_types`` is still present.
    It is dropped by SLIM_NODE_FIELDS, so an index built any later could only
    tiebreak on arity and would lose same-arity overloads.
    """
    # (class_fqn, method_name, arity) -> node ids, in extraction order
    _methods: dict[tuple[str, str, int], list] = field(default_factory=lambda: defaultdict(list))
    _classes: dict[str, str] = field(default_factory=dict)
    _fields: dict[str, str] = field(default_factory=dict)
    _file_of: dict[str, str] = field(default_factory=dict)
    # Classes with at least one DECLARED constructor, used to tell an implicit
    # default constructor apart from a genuine matching failure.
    _has_ctor: set = field(default_factory=set)

    @classmethod
    def build(cls, nodes) -> "NodeIndex":
        index = cls()
        for n in nodes:
            if n.label == "Class":
                index._classes.setdefault(n.fqn, n.id)
            elif n.label == "Field":
                index._fields.setdefault(n.fqn, n.id)
            elif n.label == "Function" and n.fqn and "#" in n.fqn:
                class_fqn, _, name = n.fqn.rpartition("#")
                key = (class_fqn, name, n.param_count or 0)
                index._methods[key].append(
                    (n.id, tuple(getattr(n, "param_types", ()) or ()))
                )
                if getattr(n, "kind", "") == "constructor":
                    index._has_ctor.add(class_fqn)
            if getattr(n, "file", ""):
                index._file_of[n.id] = n.file
        return index

    # ---- lookups -------------------------------------------------------

    def class_id(self, binary_name: str) -> str:
        source_fqn = binary_to_source_fqn(binary_name)
        return self._classes.get(source_fqn, "") if source_fqn else ""

    def field_id(self, binary_owner: str, field_name: str) -> str:
        source_fqn = binary_to_source_fqn(binary_owner)
        if not source_fqn:
            return ""
        return self._fields.get(f"{source_fqn}.{field_name}", "")

    def file_of(self, node_id: str) -> str:
        return self._file_of.get(node_id, "")

    def method_id(self, binary_owner: str, method_name: str, descriptor: str,
                  stats: MatchStats | None = None) -> str:
        """Node id for a bytecode method, or '' when it cannot be pinned.

        Arity narrows first because it is the one key both sides always agree
        on. Only when several same-arity overloads survive does this fall back
        to comparing erased parameter types — and if that still leaves more
        than one, it returns '' rather than picking.
        """
        source_fqn = binary_to_source_fqn(binary_owner)
        if not source_fqn:
            if stats:
                stats.anonymous_class += 1
            return ""
        name = method_lookup_name(source_fqn, method_name)
        try:
            arity = len(descriptor_param_types(descriptor))
        except Exception:
            return ""
        candidates = self._methods.get((source_fqn, name, arity))
        if not candidates:
            if stats:
                if source_fqn not in self._classes:
                    stats.unmatched_class += 1
                elif (method_name == "<init>" and arity == 0
                      and source_fqn not in self._has_ctor):
                    stats.implicit_default_ctor += 1
                else:
                    stats.unmatched_method += 1
            return ""
        if len(candidates) == 1:
            if stats:
                stats.matched_methods += 1
            return candidates[0][0]

        wanted = descriptor_simple_types(descriptor)
        exact = [
            node_id for node_id, ptypes in candidates
            if [normalize_type(t) for t in ptypes] == wanted
        ]
        if len(exact) == 1:
            if stats:
                stats.matched_methods += 1
            return exact[0]
        if stats:
            stats.ambiguous_method += 1
        return ""


def should_skip_method(method: MethodInfo) -> bool:
    """Compiler-generated members that must never become graph nodes or edges.

    Bridges (covariant-return forwarders) and private-member accessors are
    javac inventions with no source counterpart. Lambda bodies carry
    ACC_SYNTHETIC too but are explicitly NOT skipped: they are code someone
    wrote, merely lifted out of the enclosing method. Treating them as
    compiler noise silently discards every call made inside a lambda.
    """
    return (method.is_synthetic or method.is_bridge) and not method.is_lambda_body


def caller_needs_synthesis(info: ClassInfo, method: MethodInfo) -> bool:
    """True when no tree-sitter Function node can exist for this caller.

    These are exactly the constructs HANDOFF 4.2 lists — java.py only walks
    method/constructor declarations inside class/interface/enum/record, so
    lambda bodies, anonymous-class members and static initializers have no
    node. Phase 2.4 synthesizes one from the LineNumberTable rather than
    dropping their calls.
    """
    return info.is_anonymous or method.is_lambda_body or method.is_class_initializer

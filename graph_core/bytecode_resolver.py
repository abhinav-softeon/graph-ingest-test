"""Java CALLS / READS / WRITES read directly out of compiled bytecode.

WHY THIS BEATS EVERYTHING ELSE AVAILABLE
javac (javac_resolver.py) re-derives call bindings by recompiling the source.
Bytecode does not have to: javac already resolved every call and wrote the
answer into the class file. `invokevirtual com/acme/UserService.findById:(J)...`
names the owner, the method and the exact signature. There is nothing to infer,
so there is nothing to get wrong.

It also supplies three things no source-level pass can:

  * lambda bodies, anonymous inner classes and static initializers exist as real
    methods (HANDOFF 4.2 — java.py walks only method/constructor declarations
    inside class/interface/enum/record, so none of these get a node today, and
    JDBC callbacks live in exactly those constructs)
  * field access carries the owning class on every instruction, not just
    explicit `this.x` (HANDOFF 4.4 — WRITES=51,867 against READS=3,521, and
    that gap is the bug)
  * overloads are distinguished by descriptor rather than collapsing on arity

SCOPE AND SAFETY
Edges only. Nodes still come from tree-sitter, which owns source positions
(IMPLEMENTATION_PLAN.md D1) — the sole exception is the constructs above, which
have no source declaration to own and are synthesized here from the
LineNumberTable. A synthesized node without real line numbers is never created;
the file drops a tier instead.

Coverage is per-file. Files whose classes were not found keep javac or the
heuristic, via the same `attributed_files` contract javac_resolver uses. Never
raises into the pipeline: any failure returns available=False and the caller
keeps every edge it already had.

THE FAILURE MODE THAT MATTERS
Stale class files. They parse perfectly and produce confident, precise, wrong
edges for code that no longer exists — far worse than no edges at all. The
match-rate floor (config.bytecode_min_match_rate) exists for exactly this: if
the bytecode does not line up with the source tree, the whole pass is discarded.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .bytecode.classfile import (
    ClassFileError, ClassInfo, iter_class_files, iter_jar_classes, parse_class_file,
    type_name,
)
from .external_api import (
    classify_external, external_display, external_id, external_key,
)
from .bytecode.matcher import (
    MatchStats, NodeIndex, binary_to_source_fqn, caller_needs_synthesis, can_override,
    should_skip_method,
)
from .ids import make_id
from .models import Confidence, Edge, Node, Origin
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_ARCHIVE_EXTS = (".jar", ".war", ".ear")
_STRATEGY = "bytecode"
_EXTRACTOR = "bytecode"


@dataclass
class BytecodeReport:
    available: bool = False
    reason: str = ""
    class_sources: int = 0          # directories + archives scanned
    classes_seen: int = 0           # class files parsed
    classes_in_repo: int = 0        # those mapping to an extracted source class
    classes_failed: int = 0
    call_edges: int = 0
    field_edges: int = 0
    hierarchy_edges: int = 0        # EXTENDS + IMPLEMENTS from the class header
    override_edges: int = 0         # OVERRIDES proven by erased-descriptor match
    # Classes whose ancestor chain ran into an in-repo class this pass did not
    # parse. Their OVERRIDES answer is incomplete, so the heuristic must stay
    # enabled for their methods — tracked rather than silently tolerated.
    override_chains_truncated: int = 0
    external_edges: int = 0         # CALLS_EXTERNAL emitted
    external_nodes: int = 0         # distinct External targets synthesized
    synthesized_nodes: int = 0
    synthesis_skipped_no_lines: int = 0
    external_calls: int = 0         # callee outside the repo — Phase 4 material
    seconds: float = 0.0
    match_stats: dict = field(default_factory=dict)
    # Repo-relative paths this pass attributed, in BOTH separator spellings.
    # The heuristic must keep resolving CALLS for every file NOT in here or
    # those files silently end up with no call edges. See javac_resolver for
    # why the Windows spelling matters: RawRef.ref_file uses os.sep while these
    # derive from POSIX-style node paths, and a mismatch makes the whole pass
    # look like it worked while the heuristic quietly redid everything.
    attributed_files: set = field(default_factory=set)
    attributed_file_count: int = 0
    java_files_seen: int = 0
    # Files whose class HEADER was read here, i.e. whose EXTENDS/IMPLEMENTS are
    # compiler facts. Deliberately separate from attributed_files: that set is
    # built from method/field matching, and a class can have a clean header while
    # none of its members bind (or the reverse). Suppressing the heuristic's
    # hierarchy refs on the wrong set would drop real edges, so the two are
    # tracked independently. Same both-separator-spellings contract as
    # attributed_files.
    hierarchy_files: set = field(default_factory=set)
    hierarchy_file_count: int = 0
    # Method node ids whose full ancestor chain was walked here without hitting
    # an unparsed in-repo class. For those, this pass's OVERRIDES answer is
    # COMPLETE — including the negative answer "overrides nothing" — so
    # pipeline._derive_overrides must not add its name+param_count guesses on
    # top. Methods absent from this set keep the heuristic, so partial bytecode
    # coverage degrades gracefully instead of silently losing overrides.
    authoritative_override_methods: set = field(default_factory=set)
    # (subclass_method_id, ancestor_method_id) pairs already emitted here, so a
    # truncated-chain method that still keeps the heuristic cannot end up with
    # the same edge written twice at two different confidences.
    emitted_override_pairs: set = field(default_factory=set)

    @property
    def file_coverage(self) -> float:
        if not self.java_files_seen:
            return 0.0
        return self.attributed_file_count / self.java_files_seen

    def summary(self) -> dict:
        """Flat, JSON-safe view for the run report and the UI.

        attributed_files is deliberately excluded — it is thousands of paths and
        exists to be handed to resolve(), not to be read by a human."""
        return {
            "available": self.available,
            "reason": self.reason,
            "class_sources": self.class_sources,
            "classes_seen": self.classes_seen,
            "classes_in_repo": self.classes_in_repo,
            "call_edges": self.call_edges,
            "field_edges": self.field_edges,
            "hierarchy_edges": self.hierarchy_edges,
            "override_edges": self.override_edges,
            "override_chains_truncated": self.override_chains_truncated,
            "external_edges": self.external_edges,
            "external_nodes": self.external_nodes,
            "synthesized_nodes": self.synthesized_nodes,
            "synthesis_skipped_no_lines": self.synthesis_skipped_no_lines,
            "external_calls": self.external_calls,
            "match_rate": round(self.match_rate, 4),
            "file_coverage": round(self.file_coverage, 4),
            "attributed_file_count": self.attributed_file_count,
            "hierarchy_file_count": self.hierarchy_file_count,
            "java_files_seen": self.java_files_seen,
            "seconds": round(self.seconds, 2),
            "match_stats": dict(self.match_stats),
        }

    @property
    def match_rate(self) -> float:
        """Bytecode methods pinned to a source node, over those that should
        have been. Implicit default constructors and synthesis-bound members
        are excluded from both sides — they have no source node by design and
        would otherwise depress a number used to detect stale builds."""
        matched = self.match_stats.get("matched_methods", 0)
        missed = (self.match_stats.get("unmatched_method", 0)
                  + self.match_stats.get("unmatched_class", 0)
                  + self.match_stats.get("ambiguous_method", 0))
        total = matched + missed
        return (matched / total) if total else 0.0


def _iter_class_sources(roots: list[str]):
    """Yield ClassInfo from directories of .class files and from archives."""
    for path in roots:
        try:
            if os.path.isdir(path):
                for _rel, info in iter_class_files(path):
                    yield info
            elif path.lower().endswith(_ARCHIVE_EXTS):
                for _entry, info in iter_jar_classes(path):
                    yield info
            elif path.lower().endswith(".class"):
                yield parse_class_file(path)
        except (ClassFileError, OSError, Exception) as exc:  # noqa: BLE001
            _log.warning("[bytecode] unreadable class source %s: %s", path, exc)


def discover_class_sources(repo_root: str) -> list[str]:
    """Locate compiled output in an uploaded tree.

    Directories are collapsed to their common roots rather than listing every
    .class file: a build output tree holds tens of thousands, and iter_class_files
    walks a root far more cheaply than the caller re-walking per file.
    """
    from .discovery import discover_artifacts

    class_dirs: set[str] = set()
    archives: list[str] = []
    for art in discover_artifacts(repo_root):
        abspath = art.abspath
        if art.kind == "bytecode":
            # Climb to the package root so one walk covers the whole tree.
            d = os.path.dirname(abspath)
            parts = art.relpath.split("/")
            depth = max(0, len(parts) - 2)          # strip filename + its dir
            for _ in range(depth):
                parent = os.path.dirname(d)
                if not parent or parent == d:
                    break
                d = parent
            class_dirs.add(d)
        elif art.kind == "archive":
            archives.append(abspath)
    return sorted(class_dirs) + sorted(archives)


def _direct_ancestors(entry: tuple[str, list[str]] | None) -> list[str]:
    """Superclass plus every directly-implemented interface, as binary names."""
    if not entry:
        return []
    super_name, ifaces = entry
    out = [super_name] if super_name else []
    out.extend(ifaces)
    return out


def _synthesize_node(info: ClassInfo, method, repo: str, owner_class_id: str,
                     owner_file: str, owner_fqn: str) -> Node | None:
    """Create a Function node for a construct with no source declaration.

    Closes HANDOFF 4.2. Positions come from the LineNumberTable; if it is
    absent the node is NOT created (IMPLEMENTATION_PLAN.md invariant 1) — a node
    with fabricated positions is worse than a missing one, because every
    consumer treats file+start_line as a real place to go read code.
    """
    if not method.has_line_numbers or not owner_file:
        return None
    if method.is_lambda_body:
        kind, name = "lambda", method.name
    elif method.is_class_initializer:
        kind, name = "initializer", "<clinit>"
    elif info.is_anonymous:
        # Anonymous classes have no fqn of their own in the graph's scheme, so
        # they hang off the outer class with the binary tail preserved: the
        # '$1' is the only stable way to tell two anonymous classes apart.
        kind = "anonymous"
        name = f"{info.name.rsplit('$', 1)[-1]}.{method.name}"
    else:
        return None
    fqn = f"{owner_fqn}#{name}"
    return Node(
        id=make_id(repo, f"{fqn}{method.descriptor}", "method"),
        label="Function", name=name, fqn=fqn, repo=repo, kind=kind, lang="java",
        file=owner_file, start_line=method.start_line, end_line=method.end_line,
        param_count=method.arity, is_static=method.is_static,
        signature=f"{name}{method.descriptor}", extractor=_EXTRACTOR,
        confidence=Confidence.EXTRACTED.value,
    )


def resolve_java_bytecode(
    nodes: list, repo_root: str, repo: str,
    class_roots: list[str] | None = None,
    java_files_seen: int = 0,
    min_match_rate: float = 0.5,
    include_external_other: bool = False,
) -> tuple[list[Edge], list[Node], BytecodeReport]:
    """Read call and field edges out of compiled bytecode.

    ``nodes`` must be the FULL extracted Node objects, not slim projections:
    overload disambiguation reads ``param_types``, which SLIM_NODE_FIELDS drops.
    This is why the pass runs before pipeline.py's slim projection.

    Returns ``(edges, synthesized_nodes, report)``. On any failure the report
    says available=False and both lists are empty, so the caller keeps whatever
    it already had.
    """
    rep = BytecodeReport()
    rep.java_files_seen = java_files_seen
    t0 = time.monotonic()

    roots = class_roots if class_roots is not None else discover_class_sources(repo_root)
    if not roots:
        rep.reason = "no .class directories or archives found"
        _log.info("[bytecode] %s — Java stays on javac/heuristic", rep.reason)
        return [], [], rep
    rep.class_sources = len(roots)
    _log.info("[bytecode] scanning %s class source(s)", len(roots))

    index = NodeIndex.build(nodes)
    stats = MatchStats()
    edges: list[Edge] = []
    synthesized: list[Node] = []
    seen: set[tuple[str, str, str]] = set()
    attributed: set[str] = set()
    external_ids: set[str] = set()

    # Class hierarchy, accumulated during the single parse pass and emitted
    # after it. EXTENDS/IMPLEMENTS could in principle be emitted inline — the
    # class header names its own supertypes — but OVERRIDES needs the whole
    # ancestor CHAIN, and class files arrive in directory order, so a class is
    # routinely parsed before its own superclass. Neither can be finished until
    # every header has been seen, so both wait.
    #   binary class name -> (super_name, interfaces)
    hierarchy: dict[str, tuple[str, list[str]]] = {}
    #   binary class name -> that class's own Class node id
    own_class_ids: dict[str, str] = {}
    #   binary class name -> [(MethodInfo, method node id)] for override-capable
    #   methods only, so the dict stays proportional to real declarations
    declared_methods: dict[str, list] = {}

    for info in _iter_class_sources(roots):
        rep.classes_seen += 1

        # Only classes belonging to this repo are processed. A dependency jar
        # holds hundreds of thousands of classes whose callers are not in the
        # graph; skipping them here rather than after parsing methods is what
        # keeps 242 jars affordable.
        source_fqn = binary_to_source_fqn(info.name)
        owner_fqn = source_fqn or binary_to_source_fqn(info.outer_name)
        if not owner_fqn:
            continue
        # own_id is this class's OWN node; owner_class_id falls back to the
        # enclosing class for anonymous classes, which have no node of their own.
        # The hierarchy must only ever use own_id: attributing `Outer$1`'s
        # supertype to `Outer` would claim the outer class extends whatever the
        # callback implements, which is both wrong and load-bearing downstream.
        own_id = index.class_id(info.name)
        owner_class_id = own_id or index.class_id(info.outer_name)
        if not owner_class_id:
            continue
        rep.classes_in_repo += 1
        owner_file = index.file_of(owner_class_id)
        if owner_file:
            attributed.add(owner_file)
            attributed.add(owner_file.replace("/", os.sep))
        if own_id:
            hierarchy[info.name] = (info.super_name, info.interfaces)
            own_class_ids[info.name] = own_id

        for method in info.methods:
            if should_skip_method(method):
                stats.skipped_synthetic += 1
                continue

            if caller_needs_synthesis(info, method):
                if method.is_lambda_body:
                    stats.lambda_body += 1
                elif method.is_class_initializer:
                    stats.class_initializer += 1
                node = _synthesize_node(
                    info, method, repo, owner_class_id, owner_file, owner_fqn,
                )
                if node is None:
                    rep.synthesis_skipped_no_lines += 1
                    continue
                synthesized.append(node)
                rep.synthesized_nodes += 1
                caller_id = node.id
                edges.append(Edge(
                    "CONTAINS", owner_class_id, caller_id,
                    Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                    extractor=_EXTRACTOR, evidence_file=owner_file,
                    evidence_line=method.start_line, strategy=_STRATEGY,
                ))
            else:
                caller_id = index.method_id(info.name, method.name, method.descriptor, stats)
                if not caller_id:
                    continue

            # Only real declarations on a real named class can override, so
            # synthesized callers (lambda bodies, anonymous members, <clinit>)
            # are excluded by the `own_id` guard as well as by can_override.
            if own_id and can_override(method):
                declared_methods.setdefault(info.name, []).append((method, caller_id))

            for inv in method.invocations:
                if inv.opcode == "invokedynamic":
                    # The call-site descriptor names the functional interface
                    # method, not the code that runs; the real body is the
                    # lambda$ method, which is walked in its own right.
                    continue
                # Database facts, recorded whether or not the callee is in-repo.
                # An acquire is keyed on the RETURN type, so an in-repo pool
                # wrapper returning a Connection counts — which is how the
                # measured repo actually obtains connections (171 such factories
                # with four different naming conventions). Keying on the owner
                # alone would find almost none of them.
                # Only object returns can be a Connection, and most calls return
                # void or a primitive — so skip the descriptor parse entirely for
                # them. This runs once per invocation (3.8M times on the measured
                # repo), which is where per-call string work actually costs.
                ret = inv.descriptor.rpartition(")")[2]
                kind = classify_external(
                    inv.owner, inv.name,
                    type_name(ret) if ret[:1] == "L" else "",
                    include_other=include_external_other,
                )
                if kind:
                    eid = external_id(inv.owner, inv.name)
                    if eid not in external_ids:
                        external_ids.add(eid)
                        synthesized.append(Node(
                            id=eid, label="External",
                            name=external_display(inv.owner, inv.name),
                            fqn=external_key(inv.owner, inv.name),
                            repo="external", kind=kind, lang="java",
                            extractor=_EXTRACTOR,
                            confidence=Confidence.EXTRACTED.value,
                        ))
                        rep.external_nodes += 1
                    key = ("CALLS_EXTERNAL", caller_id, eid)
                    if key not in seen:
                        seen.add(key)
                        edges.append(Edge(
                            "CALLS_EXTERNAL", caller_id, eid,
                            Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                            extractor=_EXTRACTOR, evidence_file=owner_file,
                            evidence_line=inv.line, strategy=_STRATEGY,
                        ))
                        rep.external_edges += 1

                callee = index.method_id(inv.owner, inv.name, inv.descriptor)
                if not callee:
                    rep.external_calls += 1
                    continue
                key = ("CALLS", caller_id, callee)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(Edge(
                    "CALLS", caller_id, callee,
                    # EXTRACTED, not INFERRED: a compiler binding, not a guess.
                    Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                    extractor=_EXTRACTOR, evidence_file=owner_file,
                    evidence_line=inv.line, strategy=_STRATEGY,
                ))
                rep.call_edges += 1

            for access in method.field_accesses:
                target = index.field_id(access.owner, access.name)
                if not target:
                    stats.unmatched_field += 1
                    continue
                etype = "READS" if access.is_read else "WRITES"
                key = (etype, caller_id, target)
                if key in seen:
                    continue
                seen.add(key)
                stats.matched_fields += 1
                edges.append(Edge(
                    etype, caller_id, target,
                    Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                    extractor=_EXTRACTOR, evidence_file=owner_file,
                    evidence_line=access.line, strategy=_STRATEGY,
                ))
                rep.field_edges += 1

    rep.match_stats = stats.as_dict()
    rep.attributed_files = attributed
    rep.attributed_file_count = len({p.replace(os.sep, "/") for p in attributed})

    if not rep.classes_in_repo:
        rep.reason = (
            f"parsed {rep.classes_seen} class(es) but none matched an extracted "
            f"source class — the compiled output does not correspond to this tree"
        )
        rep.seconds = time.monotonic() - t0
        _log.warning("[bytecode] %s — Java stays on javac/heuristic", rep.reason)
        return [], [], rep

    # THE stale-build guard. Bytecode from an old build parses perfectly and
    # yields precise edges for code that no longer exists; a low match rate is
    # the only signal that this happened.
    if rep.match_rate < min_match_rate:
        rep.reason = (
            f"match rate {rep.match_rate:.0%} below {min_match_rate:.0%} "
            f"({stats.matched_methods} matched, {stats.unmatched_method} unmatched, "
            f"{stats.ambiguous_method} ambiguous) — class files look stale "
            f"relative to the source"
        )
        rep.seconds = time.monotonic() - t0
        _log.warning("[bytecode] %s — Java stays on javac/heuristic", rep.reason)
        return [], [], rep

    # Everything below runs ONLY on a pass that is going to be kept. The
    # hierarchy/OVERRIDES emission must sit AFTER both guards, not before:
    # authoritative_override_methods tells pipeline._derive_overrides to stop
    # deriving those methods, so populating it on a pass whose edges are then
    # discarded would suppress the heuristic and supply nothing in its place —
    # silently leaving those methods with no OVERRIDES at all. Discarding the
    # pass has to discard its claims too.

    # ---- class hierarchy: EXTENDS / IMPLEMENTS -------------------------
    # Straight out of the class header (JVMS 4.1 super_class / interfaces), so
    # these are compiler facts rather than the name/scope/import guesses
    # resolver.py makes for the same edge types. They matter well beyond their
    # own count (HANDOFF measured only 8,320 EXTENDS + 1,138 IMPLEMENTS on the
    # real repo): _derive_overrides walks exactly this hierarchy, so an error
    # here does not stay local, it propagates into every OVERRIDES edge and from
    # there into polymorphic dispatch.
    #
    # Emitted ADDITIVELY — the heuristic still resolves these ref types for the
    # same files. That is deliberate for now: bytecode names the supertype by
    # full binary name and looks it up by fqn, whereas the heuristic matches on
    # simple name plus imports, so on any class whose extracted fqn is spelled
    # differently than the binary name the heuristic can still find a target
    # bytecode misses. Suppressing it would trade a known-small duplicate count
    # for an unknown number of lost edges. Neo4j MERGE collapses the duplicates
    # on (type, src, dst) anyway; revisit once the overlap is measured.
    hierarchy_files: set[str] = set()
    for binary, (super_name, ifaces) in hierarchy.items():
        src_id = own_class_ids[binary]
        # Recorded for every class whose header parsed, BEFORE the per-target
        # filtering below: a class extending only out-of-repo types (HttpServlet,
        # Serializable) emits no edge, but its hierarchy is still fully known —
        # "no in-repo supertype" is a compiler fact, and the heuristic guessing
        # one anyway is precisely the case worth suppressing.
        _src_file = index.file_of(src_id)
        if _src_file:
            hierarchy_files.add(_src_file)
            hierarchy_files.add(_src_file.replace("/", os.sep))
        targets = [("EXTENDS", super_name)] + [("IMPLEMENTS", i) for i in ifaces]
        for etype, target in targets:
            if not target:
                continue
            dst_id = index.class_id(target)
            # No node for the target means it is outside the repo (extending
            # HttpServlet, implementing Serializable). Not an error: the graph
            # only holds in-repo targets, and the chain legitimately ends here.
            if not dst_id or dst_id == src_id:
                continue
            key = (etype, src_id, dst_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append(Edge(
                etype, src_id, dst_id,
                Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                extractor=_EXTRACTOR, evidence_file=index.file_of(src_id),
                strategy=_STRATEGY,
            ))
            rep.hierarchy_edges += 1

    # Set here, not up with attributed_files: that assignment runs before the
    # stale-build guards, and this loop runs after them — a discarded pass must
    # not publish a hierarchy claim (the same trap as the OVERRIDES handoff, see
    # the guard note above).
    rep.hierarchy_files = hierarchy_files
    rep.hierarchy_file_count = len({p.replace(os.sep, "/") for p in hierarchy_files})

    # ---- OVERRIDES: descriptor-exact, not name+arity -------------------
    # pipeline._derive_overrides matches a method to an ancestor's on name +
    # param_count, which cannot tell `foo(String)` from `foo(int)`. Bytecode has
    # the erased descriptor, so the same question gets an exact answer here for
    # the same cost. This is the accuracy payoff of the whole pass: OVERRIDES is
    # what polymorphic dispatch fans out over, and what a consumer joins through
    # at query time, so a false override becomes many false CALLS.
    for binary, methods in declared_methods.items():
        # Walk the ancestor chain once per class, breadth-unordered (a class has
        # one superclass but any number of interfaces, and interfaces extend
        # interfaces, so this is a DAG, not a list).
        chain: list[str] = []
        visited: set[str] = set()
        complete = True
        stack = _direct_ancestors(hierarchy.get(binary))
        while stack:
            anc = stack.pop()
            if not anc or anc in visited:
                continue
            visited.add(anc)
            chain.append(anc)
            nxt = hierarchy.get(anc)
            if nxt is None:
                # Not parsed in this pass. If it has no node either, it is a
                # third-party/JDK type and the chain genuinely ends — the answer
                # stays complete. If it IS an in-repo class we simply did not
                # parse, we cannot see its own ancestors, so the answer for this
                # class is partial and the heuristic has to keep covering it.
                if index.class_id(anc):
                    complete = False
                continue
            stack.extend(_direct_ancestors(nxt))

        for method, method_id in methods:
            for anc in chain:
                target = index.override_target(anc, method.name, method.descriptor)
                if not target or target == method_id:
                    continue
                key = ("OVERRIDES", method_id, target)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(Edge(
                    "OVERRIDES", method_id, target,
                    Confidence.EXTRACTED.value, origin=Origin.EXTRACTED.value,
                    extractor=_EXTRACTOR, evidence_file=index.file_of(method_id),
                    evidence_line=method.start_line, strategy=_STRATEGY,
                ))
                rep.override_edges += 1
                rep.emitted_override_pairs.add((method_id, target))
            if complete:
                rep.authoritative_override_methods.add(method_id)
        if not complete:
            rep.override_chains_truncated += 1

    rep.seconds = time.monotonic() - t0
    rep.available = True
    _log.info(
        "[bytecode] %s class(es) seen, %s in repo; %s CALLS + %s READS/WRITES "
        "+ %s EXTENDS/IMPLEMENTS + %s OVERRIDES, %s synthesized node(s), "
        "%s external call(s); match rate %.1f%%, file coverage %.1f%% in %.1fs",
        rep.classes_seen, rep.classes_in_repo, rep.call_edges, rep.field_edges,
        rep.hierarchy_edges, rep.override_edges,
        rep.synthesized_nodes, rep.external_calls, 100 * rep.match_rate,
        100 * rep.file_coverage, rep.seconds,
    )
    _log.info(
        "[bytecode] OVERRIDES authoritative for %s method(s) (heuristic "
        "suppressed for those); %s class(es) had a truncated ancestor chain and "
        "keep the name+arity heuristic",
        len(rep.authoritative_override_methods), rep.override_chains_truncated,
    )
    # The suppression decision for EXTENDS/IMPLEMENTS turns on how much of the
    # heuristic's hierarchy output these files duplicate. hierarchy_edges alone
    # cannot answer it (a MERGE-collapse count conflates tier duplication with
    # duplicate source files producing the same triple twice), so report the file
    # basis here and let resolve report its side against the same set.
    _log.info(
        "[bytecode] class hierarchy read for %s file(s) of %s Java file(s) seen "
        "— %s EXTENDS/IMPLEMENTS are compiler facts for those files",
        rep.hierarchy_file_count, rep.java_files_seen, rep.hierarchy_edges,
    )
    if rep.synthesis_skipped_no_lines:
        _log.info(
            "[bytecode] %s construct(s) skipped for lacking a LineNumberTable — "
            "compile with -g to capture lambdas/anonymous classes there",
            rep.synthesis_skipped_no_lines,
        )
    return edges, synthesized, rep

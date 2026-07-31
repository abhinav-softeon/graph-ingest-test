"""Ground-truth Java call bindings read straight out of compiled bytecode.

Same job as CallOracle.java, different and stronger instrument. CallOracle asks
javac to re-resolve the source; this reads the answer javac already wrote into
the class files. Every call instruction names its owner class, method and
descriptor outright, so there is nothing to infer and nothing to get wrong.

    python scripts/oracle/bytecode_oracle.py <class-root-or-jar> [...] \
        --source-root <repo> > calls.tsv
    python scripts/oracle/compare_to_graph.py calls.tsv --repo <namespace>

Output is byte-compatible with CallOracle.java (8 tab-separated columns plus
@FILE markers, STATS to stderr), so compare_to_graph.py consumes either without
modification and the two can be diffed against each other.

WHAT IS COUNTED, AND WHY

*in-repo only* — a pair is emitted only when the callee's owner is a class that
was itself parsed. The graph holds no node for `java.sql.Connection`, so
emitting calls to it would score as false negatives against a graph that was
never supposed to contain them. (Those calls are counted separately as
`resolved_external`, and they are exactly the raw material for the
CALLS_EXTERNAL work in Phase 4.)

*constructors excluded by default* — the graph models `new Foo()` as
INSTANTIATES, not CALLS, so counting `invokespecial <init>` as a call pair would
manufacture false negatives. `--constructors` includes them.

*synthetic and bridge methods skipped* — javac generates these (covariant-return
forwarders, accessor methods for private members across nested classes). They
are not code anyone wrote and have no source-level node to match.

*invokedynamic skipped* — the call-site descriptor names the functional
interface method, not the code that runs; the real target needs BootstrapMethods
resolution. Counted, never emitted.

CALLERS THE GRAPH CANNOT REPRESENT YET

Calls made from inside a lambda body, an anonymous inner class, or a static
initializer have no tree-sitter node to attribute them to (HANDOFF 4.2). They
are reported separately as `callers_without_source_node` — that number is the
measured cost of the missing-node gap, and Phase 2.4 is what closes it.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from graph_core.bytecode.classfile import (  # noqa: E402
    ClassFileError, ClassInfo, iter_class_files, iter_jar_classes, parse_class_file,
)

_ARCHIVE_EXTS = (".jar", ".war", ".ear")


def collect_classes(inputs: list[str]) -> tuple[dict[str, ClassInfo], Counter]:
    """Parse every class under the given roots/archives.

    Later definitions of the same class name lose to earlier ones, so passing a
    project's own output directory before its dependency jars keeps the
    project's version authoritative.
    """
    classes: dict[str, ClassInfo] = {}
    stats: Counter = Counter()
    for path in inputs:
        if os.path.isdir(path):
            for _rel, info in iter_class_files(path):
                stats["classes_parsed"] += 1
                classes.setdefault(info.name, info)
        elif path.lower().endswith(_ARCHIVE_EXTS):
            for _entry, info in iter_jar_classes(path):
                stats["classes_parsed"] += 1
                classes.setdefault(info.name, info)
        elif path.lower().endswith(".class"):
            try:
                info = parse_class_file(path)
            except (ClassFileError, OSError) as exc:
                stats["classes_failed"] += 1
                print(f"[oracle] skip {path}: {exc}", file=sys.stderr)
                continue
            stats["classes_parsed"] += 1
            classes.setdefault(info.name, info)
        else:
            print(f"[oracle] not a class root, jar or class file: {path}", file=sys.stderr)
    return classes, stats


def index_source_files(root: str) -> dict[str, str]:
    """Map ``package/path/File.java`` suffixes to real repo-relative paths.

    Bytecode records only the bare SourceFile name (``Fixture.java``), which
    combined with the package gives ``com/acme/Fixture.java`` — no source root
    prefix. Matching that as a suffix recovers the path the graph actually
    stores, and handles multi-module layouts without being told about them.
    """
    index: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "target", "build"}]
        for fn in filenames:
            if not fn.endswith(".java"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
            # Index by every path suffix so any package depth can be matched.
            parts = rel.split("/")
            for i in range(len(parts)):
                index.setdefault("/".join(parts[i:]), rel)
    return index


def resolve_source_path(info: ClassInfo, source_index: dict[str, str]) -> str:
    hint = info.source_path_hint
    if not hint:
        return ""
    if source_index:
        found = source_index.get(hint)
        if found:
            return found
        # Nested classes share their outer class's SourceFile, but a package
        # with no matching directory layout still resolves by bare filename.
        found = source_index.get(hint.rsplit("/", 1)[-1])
        if found:
            return found
    return hint


def caller_has_source_node(info: ClassInfo, method) -> bool:
    """Whether a tree-sitter Function node could exist for this caller.

    False for lambda bodies, anonymous-class members and <clinit> — the
    constructs HANDOFF 4.2 lists as missing. Used to size that gap, not to
    filter: these calls are real and Phase 2.4 will give them nodes.
    """
    if info.is_anonymous or method.is_lambda_body or method.is_class_initializer:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Emit ground-truth Java call bindings from compiled bytecode.",
    )
    ap.add_argument("inputs", nargs="+",
                    help="directories of .class files, .jar/.war/.ear archives, or .class files")
    ap.add_argument("--source-root", default="",
                    help="repo root, used to turn SourceFile hints into repo-relative paths")
    ap.add_argument("--constructors", action="store_true",
                    help="also emit <init> invocations (the graph models these as INSTANTIATES)")
    ap.add_argument("--include-synthetic-callers", action="store_true",
                    help="emit calls made from lambda bodies / anonymous classes / <clinit> "
                         "(no tree-sitter node exists for these yet — see HANDOFF 4.2)")
    ap.add_argument("--out", default="",
                    help="write TSV here instead of stdout")
    args = ap.parse_args()

    classes, stats = collect_classes(args.inputs)
    if not classes:
        print("[oracle] no classes parsed — check the paths", file=sys.stderr)
        return 2
    print(f"[oracle] parsed {len(classes)} distinct class(es)", file=sys.stderr)

    source_index = index_source_files(args.source_root) if args.source_root else {}
    if args.source_root:
        print(f"[oracle] indexed {len(source_index)} source path suffix(es) under "
              f"{args.source_root}", file=sys.stderr)

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    pairs: set[tuple[str, str]] = set()
    emitted_files: set[str] = set()
    try:
        for name in sorted(classes):
            info = classes[name]
            src = resolve_source_path(info, source_index)
            if src and src not in emitted_files:
                emitted_files.add(src)
                print(f"@FILE\t{src}", file=out)

            for method in info.methods:
                # Lambda bodies carry ACC_SYNTHETIC too, but unlike bridges and
                # private-member accessors they ARE code someone wrote — javac
                # merely lifted them out of the enclosing method. Dropping them
                # here would silently lose every call made inside a lambda.
                if (method.is_synthetic or method.is_bridge) and not method.is_lambda_body:
                    stats["methods_skipped_synthetic"] += 1
                    continue
                stats["methods"] += 1
                if method.has_line_numbers:
                    stats["methods_with_lines"] += 1

                source_node = caller_has_source_node(info, method)
                if not source_node:
                    stats["callers_without_source_node"] += 1

                for inv in method.invocations:
                    stats["invocations_seen"] += 1
                    if inv.opcode == "invokedynamic":
                        stats["invokedynamic_skipped"] += 1
                        continue
                    if inv.is_constructor and not args.constructors:
                        stats["constructors_skipped"] += 1
                        continue
                    if inv.owner not in classes:
                        stats["resolved_external"] += 1
                        continue
                    stats["resolved_in_repo"] += 1
                    if not source_node and not args.include_synthetic_callers:
                        stats["skipped_synthetic_caller_edges"] += 1
                        continue
                    print(
                        f"{info.name}\t{method.name}\t{method.arity}\t"
                        f"{inv.owner}\t{inv.name}\t{inv.arity}\t"
                        f"{src}\t{inv.line}",
                        file=out,
                    )
                    stats["emitted"] += 1
                    pairs.add((f"{info.name}#{method.name}", f"{inv.owner}#{inv.name}"))
    finally:
        if args.out:
            out.close()

    methods = stats["methods"] or 1
    print("=== STATS ===", file=sys.stderr)
    for key in ("classes_parsed", "classes_failed", "methods",
                "methods_skipped_synthetic", "invocations_seen",
                "resolved_in_repo", "resolved_external", "invokedynamic_skipped",
                "constructors_skipped", "callers_without_source_node",
                "skipped_synthetic_caller_edges", "emitted"):
        print(f"{key:<32} {stats[key]}", file=sys.stderr)
    print(f"{'distinct_caller_callee_pairs':<32} {len(pairs)}", file=sys.stderr)
    print(f"{'line_number_coverage':<32} "
          f"{100.0 * stats['methods_with_lines'] / methods:.1f}%", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Find out what is actually inside a repo's jars.

The decision this answers is binary and it reshapes the whole plan:

  * if a jar holds the APPLICATION's own compiled classes, bytecode resolution
    works exactly as designed — jars are just another class source, and the
    graph gets exact call bindings for whatever they cover
  * if the jars are only third-party dependencies, they are still the single
    most valuable input available: they are the CLASSPATH, which is the one
    thing javac was missing. Calls through library supertypes (Spring Data's
    save/findById, Lombok-generated accessors, anything inherited from a JAR
    interface) go from unresolvable to resolvable.

Either way the jars matter. This tells you which case you are in.

    python scripts/oracle/inspect_jars.py <repo-root> [--source-root <repo>]

Package overlap with the source tree is the discriminator: a jar whose packages
match packages that also exist as .java files is almost certainly a build of
this repo. One that shares nothing is a dependency.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from graph_core.bytecode.classfile import iter_jar_classes  # noqa: E402

_ARCHIVE_EXTS = (".jar", ".war", ".ear")


def source_packages(root: str) -> set[str]:
    """Package names declared by .java files in the tree.

    Read from the `package` declaration rather than inferred from the directory
    layout, so it works regardless of source-root arrangement.
    """
    packages: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules"}]
        for fn in filenames:
            if not fn.endswith(".java"):
                continue
            try:
                with open(os.path.join(dirpath, fn), "r", encoding="utf-8", errors="replace") as fh:
                    for _ in range(60):          # package decl is always near the top
                        line = fh.readline()
                        if not line:
                            break
                        stripped = line.strip()
                        if stripped.startswith("package ") and stripped.endswith(";"):
                            packages.add(stripped[len("package "):-1].strip())
                            break
            except OSError:
                continue
    return packages


def find_archives(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules"}]
        for fn in filenames:
            if fn.lower().endswith(_ARCHIVE_EXTS):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def top_packages(names, depth: int = 3) -> Counter:
    counts: Counter = Counter()
    for name in names:
        parts = name.split(".")
        counts[".".join(parts[:depth]) if len(parts) > depth else name.rsplit(".", 1)[0]] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Report what is inside a repo's jars.")
    ap.add_argument("root", help="repo root to scan for .jar/.war/.ear")
    ap.add_argument("--source-root", default="",
                    help="tree of .java files to compare packages against (defaults to root)")
    ap.add_argument("--top", type=int, default=25, help="how many jars to detail")
    args = ap.parse_args()

    src_root = args.source_root or args.root
    print(f"[jars] reading source packages under {src_root} ...", file=sys.stderr)
    src_pkgs = source_packages(src_root)
    print(f"[jars] {len(src_pkgs)} distinct package(s) declared in .java files", file=sys.stderr)

    archives = find_archives(args.root)
    print(f"[jars] found {len(archives)} archive(s)\n", file=sys.stderr)

    own: list[tuple[str, int, int]] = []       # (path, classes, overlapping)
    third_party: list[tuple[str, int]] = []
    failed: list[str] = []
    all_pkgs: Counter = Counter()
    total_classes = 0

    for path in archives:
        names = []
        try:
            for _entry, info in iter_jar_classes(path):
                names.append(info.name)
        except Exception as exc:                # a jar can be corrupt or not a zip
            failed.append(f"{path}: {type(exc).__name__} {exc}")
            continue
        if not names:
            continue
        total_classes += len(names)
        pkgs = {n.rsplit(".", 1)[0] for n in names if "." in n}
        all_pkgs.update(top_packages(names))
        overlap = pkgs & src_pkgs
        if overlap:
            own.append((path, len(names), len(overlap)))
        else:
            third_party.append((path, len(names)))

    rel = lambda p: os.path.relpath(p, args.root).replace("\\", "/")  # noqa: E731

    print("=" * 78)
    print(f"TOTAL: {len(archives)} archive(s), {total_classes} class(es)")
    print("=" * 78)

    print(f"\n### APPLICATION JARS — packages that also exist as .java source ({len(own)})")
    if own:
        print("    These contain THIS repo's compiled code. Bytecode resolution applies")
        print("    to them directly: exact call bindings, exact field access, lambdas")
        print("    and anonymous classes as real nodes.\n")
        for path, n, ov in sorted(own, key=lambda x: -x[1])[:args.top]:
            print(f"    {n:>7} classes  {ov:>4} pkg overlap   {rel(path)}")
    else:
        print("    NONE. No jar contains packages matching your .java sources, so the")
        print("    application itself is not compiled anywhere in this upload.")
        print("    -> Bytecode resolution has no input for YOUR code.")
        print("    -> These jars are still the CLASSPATH javac was missing, which is")
        print("       what unblocks calls through library supertypes.")

    print(f"\n### DEPENDENCY JARS ({len(third_party)})")
    for path, n in sorted(third_party, key=lambda x: -x[1])[:args.top]:
        print(f"    {n:>7} classes  {rel(path)}")
    if len(third_party) > args.top:
        print(f"    ... and {len(third_party) - args.top} more")

    print("\n### LARGEST PACKAGES ACROSS ALL JARS")
    for pkg, n in all_pkgs.most_common(20):
        marker = "  <-- also in source" if any(p.startswith(pkg) for p in src_pkgs) else ""
        print(f"    {n:>7}  {pkg}{marker}")

    if failed:
        print(f"\n### UNREADABLE ({len(failed)})")
        for line in failed[:10]:
            print(f"    {line}")

    print("\n### VERDICT")
    if own:
        print("    Application code IS compiled in these jars -> proceed with Phase 2")
        print("    bytecode resolution, sourcing classes from jars instead of a")
        print("    target/classes directory.")
    else:
        print("    Application code is NOT compiled anywhere -> Phase 2 changes shape:")
        print("    run javac with these jars as -classpath. That is Tier 1 rather than")
        print("    Tier 0, but it removes the single biggest cause of javac failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

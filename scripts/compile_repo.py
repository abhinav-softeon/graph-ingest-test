"""Compile a source-only Java repo into .class files, with no build system.

This is what turns a repo with no pom.xml into valid input for the bytecode
resolver. A build system's only contribution to compilation is telling javac
two things:

    -sourcepath   where the package roots are
    -classpath    which jars the code depends on

Both are recoverable without it. Source roots come from each file's `package`
declaration compared against its path (the same derivation CallOracle.java does),
and the classpath is simply every jar in the tree.

    python scripts/compile_repo.py <repo> --out <classes-dir>
    python scripts/compile_repo.py <repo> --out out --release 8 --encoding ISO-8859-1

PARTIAL SUCCESS IS THE EXPECTED OUTCOME, NOT A FAILURE
Some files will not compile — missing dependencies, generated sources that were
never checked in, code that needs an annotation processor that is not present.
javac still writes .class files for everything that DID compile, and the
bytecode resolver attributes per-file, so a partial compile yields a partial but
entirely correct Tier 0 layer. Anything uncovered falls through to javac or the
heuristic exactly as before.

WHAT TO WATCH
`classes produced` against your .java count is the number that matters. If it is
high, bytecode covers most of the repo. If it is near zero, read the first few
errors: they are usually one missing jar or a wrong --release, not 16,000
independent problems.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections import Counter

_PACKAGE_RE = re.compile(rb"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".idea", ".gradle"}


def java_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".java") and not fn.startswith("._"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def read_package(path: str) -> str:
    """Package declared by a .java file, or '' for the default package.

    Read as bytes so a file in an unexpected encoding cannot raise here — the
    package line is ASCII in any encoding javac accepts.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return ""
    m = _PACKAGE_RE.search(head)
    return m.group(1).decode("ascii", "replace") if m else ""


def derive_source_roots(files: list[str]) -> tuple[list[str], Counter]:
    """Source roots implied by each file's package declaration.

    javac resolves `com.foo.Bar` at `<sourcepath>/com/foo/Bar.java`, so the root
    is the file's directory with the package path stripped off the end. Passing
    the repo root instead is the bug that capped the javac oracle's coverage
    (HANDOFF 4.1): `src/main/java/com/foo/Bar.java` never resolves from the repo
    root. Deriving per file handles multi-module layouts for free.
    """
    roots: Counter = Counter()
    for path in files:
        pkg = read_package(path)
        directory = os.path.dirname(os.path.abspath(path))
        if not pkg:
            roots[directory] += 1
            continue
        suffix = os.sep.join(pkg.split("."))
        if directory.endswith(suffix):
            roots[directory[: -len(suffix)].rstrip(os.sep)] += 1
        else:
            # Package and directory disagree — common in legacy trees. The file
            # still compiles when its own directory is on the sourcepath.
            roots[directory] += 1
    return sorted(roots), roots


def find_jars(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith((".jar", ".zip")) and not fn.startswith("._"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def count_classes(out_dir: str) -> int:
    n = 0
    for _dp, _dn, filenames in os.walk(out_dir):
        n += sum(1 for f in filenames if f.endswith(".class"))
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compile a source-only Java repo into .class files without a build system.",
    )
    ap.add_argument("repo", help="repo root")
    ap.add_argument("--out", default="classes", help="output directory for .class files")
    ap.add_argument("--batch-size", type=int, default=800,
                    help="files per javac invocation (memory knob: lower it if javac OOMs)")
    ap.add_argument("--release", default="",
                    help="target Java release, e.g. 8 or 11 (omit to use the JDK default)")
    ap.add_argument("--encoding", default="UTF-8",
                    help="source encoding; legacy repos are often ISO-8859-1")
    ap.add_argument("--classpath", default="",
                    help="extra classpath entries, os.pathsep-separated (jars in the repo "
                         "are found automatically)")
    ap.add_argument("--no-jars", action="store_true", help="ignore jars found in the repo")
    ap.add_argument("--max-errors", type=int, default=8,
                    help="error lines to show per failing batch")
    ap.add_argument("--javac", default="javac",
                    help="path to javac. Use the NEWEST JDK available: javac reads "
                         "class files at or below its own version, so a JDK older "
                         "than any dependency jar fails with 'bad class file: wrong "
                         "version N, should be M'. Compiling with a new JDK does not "
                         "force a new target — pair it with --release for that.")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    files = java_files(repo)
    if not files:
        print(f"no .java files under {repo}", file=sys.stderr)
        return 2
    print(f"[compile] {len(files)} .java file(s)", file=sys.stderr)

    roots, root_counts = derive_source_roots(files)
    print(f"[compile] derived {len(roots)} source root(s):", file=sys.stderr)
    for root in sorted(root_counts, key=lambda r: -root_counts[r])[:12]:
        print(f"           {root_counts[root]:>6} files  {root}", file=sys.stderr)
    if len(roots) > 12:
        print(f"           ... and {len(roots) - 12} more", file=sys.stderr)

    jars = [] if args.no_jars else find_jars(repo)
    classpath_parts = list(jars)
    if args.classpath:
        classpath_parts += [p for p in args.classpath.split(os.pathsep) if p]
    print(f"[compile] {len(jars)} jar(s) on the classpath", file=sys.stderr)

    base_cmd = [args.javac, "-d", out_dir, "-nowarn", "-encoding", args.encoding,
                "-sourcepath", os.pathsep.join(roots)]
    if classpath_parts:
        base_cmd += ["-classpath", os.pathsep.join(classpath_parts)]
    if args.release:
        base_cmd += ["--release", args.release]
    # Errors in one file must not abort the batch's other files.
    base_cmd += ["-Xmaxerrs", "10000", "-Xmaxwarns", "0"]

    t0 = time.time()
    batches = ok = failed = 0
    first_errors: list[str] = []
    for i in range(0, len(files), args.batch_size):
        chunk = files[i:i + args.batch_size]
        batches += 1
        # An @argfile avoids blowing the command-line length limit, which 800
        # absolute paths would do on Windows.
        argfile = os.path.join(out_dir, f".sources_{i}.txt")
        with open(argfile, "w", encoding="utf-8") as fh:
            for path in chunk:
                fh.write('"' + path.replace("\\", "/") + '"\n')
        proc = subprocess.run(base_cmd + [f"@{argfile}"], capture_output=True, text=True)
        os.remove(argfile)
        if proc.returncode == 0:
            ok += 1
        else:
            failed += 1
            if len(first_errors) < args.max_errors:
                for line in (proc.stderr or "").splitlines():
                    if ": error:" in line:
                        first_errors.append(line.strip())
                        if len(first_errors) >= args.max_errors:
                            break
        done = min(i + args.batch_size, len(files))
        print(f"[compile] {done}/{len(files)} files "
              f"({batches} batch(es), {failed} with errors)", file=sys.stderr)

    produced = count_classes(out_dir)
    dt = time.time() - t0

    print("\n=== COMPILE SUMMARY ===", file=sys.stderr)
    print(f"java_files        {len(files)}", file=sys.stderr)
    print(f"source_roots      {len(roots)}", file=sys.stderr)
    print(f"jars_on_classpath {len(jars)}", file=sys.stderr)
    print(f"batches           {batches} ({ok} clean, {failed} with errors)", file=sys.stderr)
    print(f"classes_produced  {produced}", file=sys.stderr)
    print(f"seconds           {dt:.1f}", file=sys.stderr)
    if first_errors:
        print("\nfirst errors (usually ONE missing jar or a wrong --release, "
              "not N independent problems):", file=sys.stderr)
        for line in first_errors:
            print(f"  {line}", file=sys.stderr)
    if produced:
        print(f"\nNow run the graph with bytecode enabled:\n"
              f"  GRAPH_BYTECODE_CLASS_ROOTS={out_dir} python cli.py "
              f"--zip <zip> --project <name> --bytecode\n"
              f"or check what was produced first:\n"
              f"  python scripts/oracle/bytecode_oracle.py {out_dir} "
              f"--source-root {repo}", file=sys.stderr)
        return 0
    print("\nNo classes produced — read the errors above before retrying.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Precision / recall of the SINK and TAINT-SOURCE marks, against source code.

    python scripts/score_sink_db.py --repo experiment --source-root /path/to/checkout

The number STATE.md §7 says does not exist. Answers, per FUNCTION NODE:

    of the functions we marked, how many are right      -> precision
    of the functions we should have marked, how many did we get -> RECALL

WHY THIS RUNS BACKWARDS FROM THE PATH SCORER
Recall is found/(found+missed), and the graph only contains what it FOUND. Any
check that starts from a graph path or a marked node can only ever measure
precision -- there is no row to start from for a sink the catalog never saw. So
the denominator has to come from somewhere the catalog had no hand in. Here it
comes from the function's own SOURCE TEXT.

HOW THE DENOMINATOR IS BUILT WITHOUT PARSING JAVA
Iterate the graph's Function nodes for their EXTENTS only -- file, start_line,
end_line -- read those lines off disk, and decide from the text alone whether
the body issues SQL / reads untrusted input. The extents are structural (from
tree-sitter, at extraction) and carry no taint judgement, so using them does not
leak the catalog's opinion into the ground truth. Nothing about which nodes are
marked is consulted when classifying.

THE ONE BLIND SPOT, STATED RATHER THAN HIDDEN
This iterates nodes the graph HAS, so a function in a file that was never
ingested is invisible to it and cannot appear as a miss. At 99.98% file coverage
that is ~4 files. Every other miss is visible.

GROUND TRUTH IS A PROXY AND ITS ERRORS ARE YOURS TO FIND
It is a regex over source text, not labelled data. It is independent of the
catalog, which is the property that matters -- but it has its own false
positives and negatives, and no number below will confess to them. The CSVs
exist so you can read 20 rows and find out. Read them before quoting a figure.
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_core.config import neo4j_config  # noqa: E402
from graph_core.store import GraphStore  # noqa: E402

# --------------------------------------------------------------------------
# Ground truth: SINK. A statement is actually issued against a database.
#
# Deliberately NARROW. An earlier version included insert|update|delete and
# generic query helpers; those collide with List.insert, Map.update and every
# domain method called `delete`, and a loose denominator corrupts BOTH metrics
# at once -- it invents misses (recall falls) and forgives real errors
# (precision rises). When ground truth is a regex, the cost of being wide is
# paid twice, so this only contains names that belong to no other API.
#
# Bare `execute(` is excluded for the same reason: ExecutorService.execute and
# Runnable.execute are common. It is counted only alongside a DB object.
_SQL_EXEC = re.compile(
    r"\b("
    r"executeQuery|executeUpdate|executeBatch|executeLargeUpdate"
    r"|prepareStatement|prepareCall|createStatement|addBatch"
    r"|createSQLQuery|createNativeQuery|createStoredProcedureQuery"
    r")\s*\(")

# SQL built here but executed elsewhere. NOT part of the sink denominator --
# a method that concatenates a WHERE clause and returns a String is not a sink,
# it is a propagator. Counted separately because it is the population that
# explains "we marked it and the body has no execute call", and folding it into
# either bucket would misattribute those.
_SQL_LITERAL = re.compile(
    r"[\"']\s*(SELECT\s|INSERT\s+INTO\s|UPDATE\s+\w+\s+SET\s|DELETE\s+FROM\s)", re.I)

_DB_OBJECT = re.compile(
    r"\b(ResultSet|PreparedStatement|CallableStatement|Connection|DataSource"
    r"|DriverManager|SQLException)\b")
_BARE_EXECUTE = re.compile(r"\.execute\s*\(")

# --------------------------------------------------------------------------
# Ground truth: TAINT SOURCE.
#
# Split in two because whether the second tier counts is a POLICY choice, not a
# fact, and blending them would bake one answer in. HTTP input is untrusted by
# definition. A value read back out of your own database is untrusted only if
# you accept second-order taint -- which this catalog does (STATE.md §2 lists
# ResultSet.getString as a curated source), but a reader may not. Both numbers
# are reported so the choice stays visible.
_SRC_HTTP = re.compile(
    r"\b(getParameter|getParameterValues|getParameterMap|getHeader|getHeaders"
    r"|getCookies|getQueryString|getRequestURI|getRequestURL|getPathInfo"
    r"|getRemoteUser|getPathTranslated)\s*\(")
_SRC_JSP_EL = re.compile(r"\$\{\s*(param|header|cookie)\b")
_SRC_SECOND_ORDER = re.compile(
    r"\bresultSet|\brs\s*\.\s*get(String|Int|Long|Object|Date|Double|BigDecimal)\s*\("
    r"|\bResultSet\b[^;]{0,80}\.get(String|Int|Long|Object)\s*\(", re.I)

_LINE_COMMENT = re.compile(r"//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip(src: str) -> str:
    """Drop comments. A commented-out executeQuery is not a sink, and counting
    it as one would invent a miss for a function that correctly was not marked."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def truth_sink(body: str) -> bool:
    clean = _strip(body)
    if _SQL_EXEC.search(clean):
        return True
    return bool(_BARE_EXECUTE.search(clean) and _DB_OBJECT.search(clean))


def truth_source(body: str, second_order: bool) -> bool:
    clean = _strip(body)
    if _SRC_HTTP.search(clean) or _SRC_JSP_EL.search(clean):
        return True
    return bool(second_order and _SRC_SECOND_ORDER.search(clean))


class SourceIndex:
    """Resolve a graph `file` value to text on disk.

    Basename fallback ALWAYS finds something when a tree has duplicate class
    names, so an unresolved count of 0 is not evidence resolution worked -- it
    can equally mean every lookup landed on a same-named file in another source
    tree and the line ranges were read from the wrong body. How each file was
    found is therefore counted and reported, not assumed.
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root.rstrip("/\\"))
        self._by_basename: dict[str, list[str]] | None = None
        self.stats: collections.Counter = collections.Counter()
        self.ambiguous: dict[str, int] = {}
        self.unresolved: list[str] = []

    def _index(self) -> dict[str, list[str]]:
        if self._by_basename is None:
            idx: dict[str, list[str]] = {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in
                               (".git", "venv", "node_modules", "build", "target")]
                for fn in filenames:
                    if fn.endswith((".java", ".jsp", ".jspf")):
                        idx.setdefault(fn, []).append(os.path.join(dirpath, fn))
            self._by_basename = idx
        return self._by_basename

    def lines(self, path: str) -> list[str] | None:
        norm = (path or "").replace("\\", "/")
        rel = norm.lstrip("/")
        if not rel:
            self.stats["unresolved"] += 1
            return None
        cands = [path, os.path.join(self.root, rel)]

        # The root's own trailing segments repeated at the head of the stored
        # path. --source-root .../ARAMEX-Source with files stored as
        # "ARAMEX-Source/Source/..." joins to ".../ARAMEX-Source/ARAMEX-Source/
        # Source/...", which does not exist -- and every lookup then fell
        # through to basename matching with nothing reporting that it had.
        root_parts = [p for p in self.root.replace("\\", "/").split("/") if p]
        rel_parts = [p for p in rel.split("/") if p]
        for n in range(min(len(root_parts), len(rel_parts)), 0, -1):
            if root_parts[-n:] == rel_parts[:n]:
                cands.append(os.path.join(self.root, *rel_parts[n:]))
                break
        for marker in ("/src/", "/webapp/", "/WebContent/", "/WEB-INF/",
                       "/Source/", "/FE_SOURCE/", "/BE_SOURCE/"):
            i = norm.find(marker)
            if i > 0:
                cands.append(os.path.join(self.root, norm[i + 1:]))

        hit, how = None, ""
        for c in cands:
            if c and os.path.isfile(c):
                hit, how = c, "exact"
                break
        if hit is None:
            matches = self._index().get(os.path.basename(norm), [])
            if matches:
                hit, how = matches[0], "basename"
                if len(matches) > 1:
                    self.ambiguous[norm] = len(matches)
        if hit is None:
            self.stats["unresolved"] += 1
            if len(self.unresolved) < 5:
                self.unresolved.append(norm)
            return None
        self.stats[how] += 1
        try:
            with open(hit, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            self.stats["unreadable"] += 1
            return None


class Confusion:
    """TP/FP/FN/TN plus the metrics, so both questions report identically."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tp = self.fp = self.fn = self.tn = 0

    def add(self, marked: bool, truth: bool) -> str:
        if marked and truth:
            self.tp += 1
            return "TP"
        if marked and not truth:
            self.fp += 1
            return "FP"
        if truth and not marked:
            self.fn += 1
            return "FN"
        self.tn += 1
        return "TN"

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def report(self) -> None:
        total = self.tp + self.fp + self.fn + self.tn
        print(f"\n=== {self.name} ===")
        print(f"  marked and right (TP)   {self.tp:>8,}")
        print(f"  marked, source says no  {self.fp:>8,}   (FP)")
        print(f"  MISSED (FN)             {self.fn:>8,}   <-- recall gap")
        print(f"  correctly silent (TN)   {self.tn:>8,}")
        print(f"\n  precision  {self.precision:>7.1%}   of what we marked, this much is real")
        print(f"  RECALL     {self.recall:>7.1%}   of what is really there, this much we found")
        print(f"  F1         {self.f1:>7.1%}")
        # Accuracy is printed because it was asked for, with the caveat that
        # makes it safe to read: on this distribution TN is ~97% of all
        # functions, so accuracy measures the ability to say "not a sink" about
        # a function that obviously isn't. It moves by a fraction of a point no
        # matter how bad recall gets, and should never be quoted alone.
        acc = (self.tp + self.tn) / total if total else 0.0
        print(f"  accuracy   {acc:>7.1%}   dominated by {self.tn/total:.0%} true "
              f"negatives -- do not quote this alone")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--second-order", action="store_true",
                    help="count ResultSet getters as taint sources (the catalog "
                         "does). Off by default so the HTTP-only number, which "
                         "nobody disputes, is what you see first.")
    ap.add_argument("--batch", type=int, default=300, help="files per query")
    ap.add_argument("--out-prefix", default="score")
    a = ap.parse_args()

    store = GraphStore(neo4j_config())
    try:
        files = [r["file"] for r in store.read(
            "MATCH (f:Function {repo: $repo}) WHERE f.file IS NOT NULL "
            "RETURN DISTINCT f.file AS file", repo=a.repo)]
        print(f"files with functions: {len(files):,}")
        if not files:
            print("  Nothing to score -- wrong --repo?")
            return

        index = SourceIndex(a.source_root)
        sink_cm = Confusion("SINK  (executes SQL)")
        src_cm = Confusion("TAINT SOURCE  (reads untrusted input)")
        by_lang: dict[str, Confusion] = {}
        # Where a missed sink dies downstream. A miss at the catalog is fixed by
        # adding a signature; a node that IS marked but never reaches the
        # universe is a different bug with a different fix, and one blended
        # recall number would hide which one you have.
        funnel = collections.Counter()
        sql_builders = 0
        mismatches: list[dict] = []
        scanned = 0

        for i in range(0, len(files), a.batch):
            chunk = files[i:i + a.batch]
            rows = store.read(
                """
                MATCH (f:Function {repo: $repo}) WHERE f.file IN $files
                OPTIONAL MATCH (f)-[:CALLS_EXTERNAL]->(x:External)
                RETURN f.id AS id, f.fqn AS fqn, f.file AS file, f.lang AS lang,
                       f.start_line AS s, f.end_line AS e,
                       coalesce(f.taint_source, false) AS taint_source,
                       f.taint_categories AS cats,
                       coalesce(f.reaches_sink, false) AS reaches_sink,
                       coalesce(f.from_entry, false) AS from_entry,
                       collect(DISTINCT x.kind) AS ext_kinds
                """, repo=a.repo, files=chunk)

            cache: dict[str, list[str] | None] = {}
            for r in rows:
                path = r["file"]
                if path not in cache:
                    cache[path] = index.lines(path)
                lines = cache[path]
                if lines is None:
                    continue
                s = int(r["s"] or 0)
                e = int(r["e"] or 0)
                if e <= 0 or e < s:
                    e = min(len(lines), s + 200)
                body = "\n".join(lines[max(0, s - 1):e])
                scanned += 1

                cats = r["cats"] or []
                kinds = [k for k in (r["ext_kinds"] or []) if k]
                # What the GRAPH claims, from either marking path: the bytecode
                # edge, or the catalog's ingest-time category. Either counts as
                # "we marked it" -- the question here is whether the system
                # knows, not which mechanism told it.
                marked_sink = ("db_execute" in kinds
                               or any("CWE-89" in str(c) for c in cats))
                t_sink = truth_sink(body)
                v_sink = sink_cm.add(marked_sink, t_sink)

                marked_src = bool(r["taint_source"])
                t_src = truth_source(body, a.second_order)
                v_src = src_cm.add(marked_src, t_src)

                lang = (r["lang"] or "?").lower()
                by_lang.setdefault(lang, Confusion(f"SINK / lang={lang}")).add(
                    marked_sink, t_sink)

                if t_sink and not marked_sink:
                    if r["reaches_sink"]:
                        funnel["missed by catalog, but reaches_sink via another route"] += 1
                    else:
                        funnel["missed by catalog AND outside the universe"] += 1
                if marked_sink and not t_sink and _SQL_LITERAL.search(_strip(body)):
                    sql_builders += 1

                if v_sink in ("FP", "FN") or v_src in ("FP", "FN"):
                    if len(mismatches) < 20000:
                        mismatches.append({
                            "sink_verdict": v_sink, "source_verdict": v_src,
                            "fqn": r["fqn"], "file": path, "line": s,
                            "lang": lang, "ext_kinds": ",".join(kinds),
                            "taint_categories": ",".join(str(c) for c in cats),
                            "reaches_sink": r["reaches_sink"],
                            "from_entry": r["from_entry"],
                        })
            print(f"  scanned {scanned:,} functions...", end="\r")

        print(f"\nfunctions scored: {scanned:,}")
        print("\n=== how sink files were located ===")
        for how, n in index.stats.most_common():
            print(f"  {n:>8,}  {how}")
        if index.stats.get("basename"):
            print("  WARNING: `basename` means the stored path did not resolve under\n"
                  "  --source-root and a same-named file was used instead. With duplicate\n"
                  "  class names across trees that is the WRONG body and every number\n"
                  "  below is noise. Fix --source-root before reading further.")
        if index.ambiguous:
            print(f"  {len(index.ambiguous):,} basename lookups had multiple candidates "
                  f"(first-wins applied)")
        if index.unresolved:
            print(f"  unresolved examples: {index.unresolved[:2]}")

        sink_cm.report()
        src_cm.report()
        if not a.second_order:
            print("  (HTTP inputs only. The catalog also treats ResultSet getters as\n"
                  "   sources; re-run with --second-order for that denominator.)")

        print("\n=== sink recall by language ===")
        print(f"  {'lang':<8} {'recall':>8} {'precision':>10} {'missed':>8}")
        for lang, cm in sorted(by_lang.items(), key=lambda kv: -kv[1].fn):
            if cm.tp + cm.fn == 0:
                continue
            print(f"  {lang:<8} {cm.recall:>7.1%} {cm.precision:>10.1%} {cm.fn:>8,}")
        print("  JSP is compiled by the container and has no class file, so it gets\n"
              "  the heuristic mark only -- expect it to be the weak row, and it is\n"
              "  also where this repo's JDBC calls live.")

        if funnel:
            print("\n=== where the missed sinks die ===")
            for k, n in funnel.most_common():
                print(f"  {n:>8,}  {k}")

        if sql_builders:
            print(f"\n{sql_builders:,} of the false positives contain a SQL string "
                  f"literal but no execute call.\n  Those are SQL BUILDERS -- "
                  f"propagators, not sinks. Arguably mismarked rather than wrong.")

        out = f"{a.out_prefix}_mismatches.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(mismatches[0].keys())
                               if mismatches else ["sink_verdict"])
            w.writeheader()
            w.writerows(mismatches)
        print(f"\nwrote {out} ({len(mismatches):,} rows). Filter sink_verdict=FN for\n"
              f"the misses. Read 20 by hand before quoting recall -- the ground\n"
              f"truth is a regex and its errors will not announce themselves.")
    finally:
        store.close()


if __name__ == "__main__":
    main()

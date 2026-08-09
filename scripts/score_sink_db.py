"""Does the sink of each enumerated path ACTUALLY touch the database in source?

    python scripts/score_sink_db.py --repo experiment --source-root /path/to/checkout
    python scripts/score_sink_db.py --repo experiment --source-root ... --kinds db_execute

The one number STATE.md §7 says does not exist yet, scoped down to the only
question that can be answered without a dataflow engine or labelled data:

    of the N paths returned, how many end in a function whose SOURCE really
    issues a database operation, and how many do not?

WHY THIS IS NOT CIRCULAR
`sink_kinds` on a path comes from the catalog matching bytecode -- it is the
claim being tested. So the verdict here is computed by reading the sink
function's own text out of the checkout and looking for JDBC/ORM calls. Graph
says db_execute, source says no SQL anywhere in the body -> that is a false
positive, and it is counted as one.

The sink is the right place to check and the check is tight, because
sink_paths() defines a sink as a function with its OWN `CALLS_EXTERNAL` edge.
The call is in that function's body by construction, so "read start_line..
end_line and look" is an exact test, not a proxy for one.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
Measures: the sink end of the path -- is the destination really a DB operation.
Does NOT measure: whether untrusted data reaches it. That is the LLM's job and
no number here speaks to it. A path can score `sql` below and still be
unexploitable.

Three verdicts, because two would hide the finding that matters:
  sql       body issues a statement (executeQuery/prepareStatement/createQuery..)
  adjacent  body only handles DB objects -- ResultSet.next, Connection.close,
            Class.forName(driver). Real DB code, but no SQL is executed here, so
            a SQL-injection claim about it is wrong even though the kind matched.
  none      no database anything. Straight false positive.

`adjacent` is the whole reason for a 3-way split: it is exactly the db_other
population that filled the old 2,000-path budget, and lumping it in with either
neighbour would make the result look better or worse than it is.
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import paths  # noqa: E402
from graph_core.config import neo4j_config  # noqa: E402
from graph_core.store import GraphStore  # noqa: E402

# A statement is actually issued. Deliberately does NOT include a bare
# `execute(` -- ExecutorService.execute, Runnable.execute and half the servlet
# frameworks use that name, and a regex cannot tell them apart. Bare execute is
# counted separately as `ambiguous_execute` and reported, rather than being
# quietly folded into either bucket.
_SQL = re.compile(
    r"\b("
    r"executeQuery|executeUpdate|executeBatch|executeLargeUpdate"
    r"|prepareStatement|prepareCall|createStatement|addBatch"
    r"|createQuery|createSQLQuery|createNativeQuery|createStoredProcedureQuery"
    r"|getResultList|getSingleResult|executeSql|queryForObject|queryForList"
    r"|selectList|selectOne|insert|update|delete"
    r")\s*\(")

# Handles DB objects but issues nothing.
_ADJACENT = re.compile(
    r"\b("
    r"ResultSet|PreparedStatement|CallableStatement|Statement|Connection"
    r"|DataSource|getConnection|isClosed|commit|rollback|setAutoCommit"
    r"|Class\.forName|DriverManager|getMetaData|SQLException"
    r")\b")

_BARE_EXECUTE = re.compile(r"\.execute\s*\(")

# Comment/string stripping. Crude on purpose: a `"select * from"` literal inside
# a function that never executes it is still evidence of DB intent, but a
# COMMENTED-OUT executeQuery is not, and the second one produces false
# positives in this scorer while the first does not.
_LINE_COMMENT = re.compile(r"//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def classify(body: str) -> tuple[str, str]:
    """(verdict, evidence) for one sink function body."""
    clean = _strip(body)
    m = _SQL.search(clean)
    if m:
        return "sql", m.group(1)
    if _BARE_EXECUTE.search(clean) and _ADJACENT.search(clean):
        return "sql", "execute( with DB objects in scope"
    m = _ADJACENT.search(clean)
    if m:
        return "adjacent", m.group(1)
    if _BARE_EXECUTE.search(clean):
        return "none", "bare execute(, no DB objects -- probably not a DB call"
    return "none", ""


class SourceIndex:
    """Resolve a graph `file` value to real text on disk.

    Stored paths may be absolute from the ingest machine, repo-relative, or
    inside an unpacked upload dir, so try all three before giving up. Basename
    fallback is built once and only for files the graph actually points at --
    walking 43k files per sink would dominate the runtime.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self._by_basename: dict[str, list[str]] | None = None
        self.cache: dict[str, list[str] | None] = {}
        self.unresolved: set[str] = set()

    def _index(self) -> dict[str, list[str]]:
        if self._by_basename is None:
            idx: dict[str, list[str]] = {}
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames
                               if d not in (".git", "venv", "node_modules", "build", "target")]
                for fn in filenames:
                    if fn.endswith((".java", ".jsp", ".jspf")):
                        idx.setdefault(fn, []).append(os.path.join(dirpath, fn))
            self._by_basename = idx
        return self._by_basename

    def lines(self, path: str) -> list[str] | None:
        if path in self.cache:
            return self.cache[path]
        cands = [path, os.path.join(self.root, path.replace("\\", "/").lstrip("/"))]
        norm = path.replace("\\", "/")
        for marker in ("/src/", "/webapp/", "/WebContent/"):
            i = norm.find(marker)
            if i > 0:
                cands.append(os.path.join(self.root, norm[i + 1:]))
        hit = None
        for c in cands:
            if c and os.path.isfile(c):
                hit = c
                break
        if hit is None:
            for c in self._index().get(os.path.basename(norm), []):
                hit = c
                break
        if hit is None:
            self.unresolved.add(path)
            self.cache[path] = None
            return None
        try:
            with open(hit, "r", encoding="utf-8", errors="replace") as fh:
                out = fh.read().splitlines()
        except OSError:
            out = None
        self.cache[path] = out
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--source-root", required=True,
                    help="checkout the graph was built from -- the sink bodies "
                         "are read from here, and that is what makes this a "
                         "ground-truth check rather than a restatement of the "
                         "graph's own claim.")
    ap.add_argument("--kinds", nargs="*", default=None)
    ap.add_argument("--min-hops", type=int, default=1)
    ap.add_argument("--max-depth", type=int, default=None)
    ap.add_argument("--limit", type=int, default=paths.DEFAULT_PATH_LIMIT)
    ap.add_argument("--any-entry", action="store_true")
    ap.add_argument("--out", default="sink_db_audit.csv",
                    help="per-path verdicts, so the automated call can be "
                         "spot-checked by hand. The regex is itself unvalidated; "
                         "reading 20 rows of this is how you find out if it lies.")
    ap.add_argument("--show", type=int, default=10)
    a = ap.parse_args()

    store = GraphStore(neo4j_config())
    try:
        hubs = paths.find_hubs(store, a.repo)
        kw = dict(repo=a.repo, sink_kinds=a.kinds, limit=a.limit,
                  hub_ids=[h["id"] for h in hubs], min_depth=a.min_hops,
                  from_taint_source=not a.any_entry)
        if a.max_depth is not None:
            kw["max_depth"] = a.max_depth
        rows = paths.sink_paths(store, **kw)
        truncated = len(rows) >= a.limit
        rows = paths.dedupe_paths(rows)
        print(f"paths after dedupe: {len(rows):,}")
        if truncated:
            print("  WARNING: the path limit was hit -- this scores a TRUNCATED "
                  "set. The ratio is still meaningful; the absolute counts are not.")
        if not rows:
            return

        # end_line is not in sink_paths' RETURN, and adding it there would change
        # the analysis query to serve a scorer. Fetched separately instead.
        sink_ids = sorted({r.get("ids")[-1] for r in rows if r.get("ids")})
        extent = {
            r["id"]: (r["file"], r["start_line"] or 0, r["end_line"] or 0)
            for r in store.read(
                "MATCH (f:Function) WHERE f.id IN $ids "
                "RETURN f.id AS id, f.file AS file, f.start_line AS start_line, "
                "f.end_line AS end_line", ids=sink_ids)
        }
        print(f"distinct sink functions: {len(extent):,}")

        index = SourceIndex(a.source_root)
        verdict_of: dict[str, tuple[str, str]] = {}
        for sid, (path, s, e) in extent.items():
            lines = index.lines(path or "")
            if lines is None:
                verdict_of[sid] = ("unresolved", "source file not found")
                continue
            if e <= 0 or e < s:
                e = min(len(lines), s + 200)   # missing end_line: bounded window
            body = "\n".join(lines[max(0, s - 1):e])
            verdict_of[sid] = classify(body)

        by_path = collections.Counter()
        by_sink_fn = collections.Counter()
        cross = collections.Counter()
        for r in rows:
            sid = r["ids"][-1]
            v, _ = verdict_of.get(sid, ("unresolved", ""))
            by_path[v] += 1
            claimed = ",".join(sorted(r.get("sink_kinds") or [])) or "?"
            cross[(claimed, v)] += 1
        for sid, (v, _) in verdict_of.items():
            by_sink_fn[v] += 1

        total = sum(by_path.values())
        print("\n=== does the sink actually touch the DB, per PATH ===")
        for v in ("sql", "adjacent", "none", "unresolved"):
            n = by_path.get(v, 0)
            if n:
                print(f"  {n:>7,}  {n/total:>6.1%}  {v}")
        tot_fn = sum(by_sink_fn.values())
        print("\n=== same, per distinct SINK FUNCTION (the unit that can be wrong) ===")
        for v in ("sql", "adjacent", "none", "unresolved"):
            n = by_sink_fn.get(v, 0)
            if n:
                print(f"  {n:>7,}  {n/tot_fn:>6.1%}  {v}")

        scored = total - by_path.get("unresolved", 0)
        if scored:
            print(f"\nsink-end precision, strict (sql only):      "
                  f"{by_path.get('sql', 0)/scored:.1%}")
            print(f"sink-end precision, loose (sql + adjacent): "
                  f"{(by_path.get('sql', 0)+by_path.get('adjacent', 0))/scored:.1%}")

        print("\n=== graph's claimed kind  x  what the source says ===")
        print(f"  {'claimed kind':<34} {'verdict':<11} {'paths':>7}")
        for (claimed, v), n in cross.most_common(15):
            print(f"  {claimed[:33]:<34} {v:<11} {n:>7,}")

        if index.unresolved:
            print(f"\n{len(index.unresolved)} file path(s) did not resolve under "
                  f"--source-root; first: {sorted(index.unresolved)[0]}")
            print("  If this is most of them, --source-root is wrong and every "
                  "number above is meaningless. Check it before reading further.")

        print(f"\nworst offenders (claimed a DB kind, source has no DB at all):")
        shown = 0
        for r in rows:
            if shown >= a.show:
                break
            sid = r["ids"][-1]
            v, ev = verdict_of.get(sid, ("", ""))
            if v != "none":
                continue
            print(f"  [{','.join(r.get('sink_kinds') or [])}] {r.get('sink_fqn')}")
            print(f"      {r.get('sink_file')}:{r.get('sink_line')}  "
                  f"names={','.join((r.get('sink_names') or [])[:3])}")
            shown += 1
        if not shown:
            print("  none -- every scored sink had at least DB-adjacent code.")

        with open(a.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["verdict", "evidence", "claimed_kinds", "sink_names",
                        "sink_fqn", "sink_file", "sink_line", "hops", "entry_fqn"])
            for r in rows:
                sid = r["ids"][-1]
                v, ev = verdict_of.get(sid, ("unresolved", ""))
                w.writerow([v, ev, ",".join(r.get("sink_kinds") or []),
                            ",".join(r.get("sink_names") or []),
                            r.get("sink_fqn"), r.get("sink_file"),
                            r.get("sink_line"), r.get("hops"), r.get("entry_fqn")])
        print(f"\nwrote {a.out} -- spot-check 20 rows by hand before trusting the ratio.")
    finally:
        store.close()


if __name__ == "__main__":
    main()

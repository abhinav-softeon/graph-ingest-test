"""Database vulnerability detection from Pass A summaries.

THE GRAPH-ONLY LEAK DETECTOR USED TO LIVE HERE AND WAS REMOVED
It asked "is a release reachable over trusted edges within N hops?", which cannot
express the thing that actually matters — whether the release runs on EVERY path.
`CALLS_EXTERNAL -> db_release` proves a close() exists SOMEWHERE in the function:

    conn = pool.getConnection();
    stmt = conn.createStatement();   // throws -> conn never closed
    conn.close();                    // present, so the graph saw a release

The graph called that clean. It is a leak. Measured on the corpus the detector
scored 47% recall against ground truth, and the 8 misses were exactly the
exception-path cases — so it was strictly dominated by `db.released_in_finally`
below, which reads the control flow directly.

WHAT WAS LOST WITH IT
It was also the only independent check on the MODEL's recall: a `db_acquire` the
bytecode classifier saw and the summary never mentioned. Nothing measures that
now. If Pass A silently stops reporting acquisitions, these detectors go quiet and
nothing says so.
"""
from __future__ import annotations

import json

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)




def leaks_from_summaries(store, repo: str) -> list[dict]:
    """Leaks the SUMMARIES report — including those the graph calls clean.

    The precision half. `released_in_finally = false` while `acquires = true` is
    the classic exception-path leak: the close() is present (so the graph is
    satisfied) but unreachable when anything between acquire and close throws.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary_json IS NOT NULL AND f.summary_hash = f.body_hash
        RETURN f.id AS id, f.fqn AS fqn, f.file AS file,
               f.start_line AS line, f.summary_json AS summary
        """,
        repo=repo,
    )
    out = []
    for row in rows:
        try:
            s = json.loads(row["summary"])
        except (ValueError, TypeError):
            continue
        db = s.get("db") or {}
        if not db.get("acquires"):
            continue
        leaked = db.get("resources_leaked") or []
        if db.get("released_in_finally") and not leaked:
            continue  # released on every path — genuinely clean
        out.append({
            "id": row["id"], "fqn": row["fqn"], "file": row["file"],
            "line": row["line"],
            "releases": bool(db.get("releases")),
            "released_in_finally": bool(db.get("released_in_finally")),
            "resources_leaked": leaked,
            "why": ("no release at all" if not db.get("releases")
                    else "release is not on every path (no finally / try-with-resources)"),
            "does": s.get("does", ""),
        })
    _log.info("[dbvuln] summary-reported leaks: %s", len(out))
    return out


def injection_candidates(store, repo: str) -> list[dict]:
    """Functions executing SQL built from a non-constant.

    Ranked by whether a parameter demonstrably reaches the query: a dynamic query
    assembled purely from constants and internal state is a smell, one fed by a
    parameter is a candidate vulnerability. `params[].flows_to` carrying 'sql' is
    what separates the two, and it exists precisely so this ranking is possible.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary_json IS NOT NULL AND f.summary_hash = f.body_hash
        RETURN f.id AS id, f.fqn AS fqn, f.file AS file,
               f.start_line AS line, f.summary_json AS summary
        """,
        repo=repo,
    )
    out = []
    for row in rows:
        try:
            s = json.loads(row["summary"])
        except (ValueError, TypeError):
            continue
        db = s.get("db") or {}
        if not (db.get("executes_sql") and db.get("sql_is_dynamic")):
            continue
        tainted = [p.get("name") for p in (s.get("params") or [])
                   if "sql" in (p.get("flows_to") or []) and not p.get("validated")]
        out.append({
            "id": row["id"], "fqn": row["fqn"], "file": row["file"],
            "line": row["line"],
            "params_reaching_sql": [p for p in tainted if p],
            # A parameter reaching a dynamically-built query unvalidated is the
            # high-severity shape; no such parameter means the dynamic part comes
            # from internal state and is a lower-priority review item.
            "severity": "high" if tainted else "review",
            "does": s.get("does", ""),
        })
    out.sort(key=lambda r: (r["severity"] != "high", r["file"], r["line"] or 0))
    _log.info("[dbvuln] injection candidates: %s (%s high)",
              len(out), sum(1 for r in out if r["severity"] == "high"))
    return out



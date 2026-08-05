"""Neo4j reads and writes for the analysis passes.

WHAT LIVES WHERE
Summaries are stored ON the Function node next to `body_hash`, not in a side table.
That co-location is the point: the invalidation key and the cached value are read
and written by the same query, so they cannot drift, and Pass B can join summaries
to paths in one Cypher round trip instead of a second store lookup per hop.

THE CACHE KEY IS body_hash, AND THAT IS WHY THIS IS AFFORDABLE
`body_hash` is a sha1 of the function's source text, computed at extraction time
and already persisted. A function whose body_hash still matches its stored
`summary_hash` is skipped entirely — not re-sent, not re-parsed, not re-billed.
Because summaries are INDEPENDENT (no callee dependency), a change to one function
invalidates only that function; there is no cascade and no fixpoint to iterate.

Node identity is content-independent (`make_id(repo, fqn, kind)`), so an edit
changes body_hash while keeping id — which is exactly what makes incremental
re-summarization work across edits.
"""
from __future__ import annotations

import json
import time

from . import contract
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Only these labels are ever summarized. External nodes have no file and no source
# by construction — they are path termini and seed markers, never analysis targets.
_FUNCTION_LABEL = "Function"


def files_with_functions(store, repo: str, langs: list[str] | None = None) -> list[dict]:
    """Every file holding at least one Function, with its functions attached.

    Grouped by file because Pass A sends one file per call: the unit of work is the
    file, not the function. Ordered by start_line so the request lists functions in
    the order they appear in the source the model is reading.
    """
    lang_filter = "AND f.lang IN $langs" if langs else ""
    rows = store.read(
        f"""
        MATCH (f:{_FUNCTION_LABEL} {{repo: $repo}})
        WHERE f.file IS NOT NULL AND f.body_hash IS NOT NULL {lang_filter}
        RETURN f.file AS file, f.lang AS lang,
               collect({{
                 id: f.id, name: f.name, fqn: f.fqn, signature: f.signature,
                 start_line: f.start_line, end_line: f.end_line,
                 body_hash: f.body_hash, kind: f.kind,
                 summary_hash: f.summary_hash,
                 sig_schema_version: f.sig_schema_version
               }}) AS functions
        ORDER BY file
        """,
        repo=repo, langs=langs or [],
    )
    out = []
    for row in rows:
        funcs = sorted(row["functions"], key=lambda f: f.get("start_line") or 0)
        out.append({"file": row["file"], "lang": row["lang"], "functions": funcs})
    return out


def graph_facts(store, repo: str, function_ids: list[str]) -> dict[str, dict]:
    """Ground truth per function, for the model to be TOLD rather than infer.

    Three things the graph knows exactly and a reader would have to guess at:
      * callees      — resolved call targets, filtered to trustworthy strategies.
                       Also the cross-check set for hallucinated `calls`.
      * externals    — already-classified out-of-repo calls with their kind, so
                       `db_execute` on a path is a fact, not an opinion.
      * fields       — READS/WRITES with the field's DECLARED TYPE via OF_TYPE.
                       This is the one that stops `x.close()` being misread: the
                       type says Connection or InputStream, and the field is often
                       declared in a parent class the reader never opens.
    """
    if not function_ids:
        return {}
    rows = store.read(
        """
        UNWIND $ids AS fid
        MATCH (f:CodeNode {id: fid})
        OPTIONAL MATCH (f)-[c:CALLS]->(callee:Function)
          WHERE c.strategy = 'bytecode'
             OR c.strategy STARTS WITH 'receiver_type'
             OR c.strategy STARTS WITH 'same_'
        OPTIONAL MATCH (f)-[:CALLS_EXTERNAL]->(x:External)
        OPTIONAL MATCH (f)-[rw:READS|WRITES]->(fld:Field)
        OPTIONAL MATCH (fld)-[:OF_TYPE]->(ft:Class)
        RETURN fid AS id,
               collect(DISTINCT callee.name) AS callees,
               collect(DISTINCT {name: x.name, kind: x.kind}) AS externals,
               collect(DISTINCT {field: fld.name, type: coalesce(ft.fqn, ft.name),
                                 access: type(rw)}) AS fields
        """,
        ids=function_ids,
    )
    facts: dict[str, dict] = {}
    for row in rows:
        facts[row["id"]] = {
            "callees": sorted(n for n in row["callees"] if n),
            "externals": [e for e in row["externals"] if e.get("name")],
            "fields": [f for f in row["fields"] if f.get("field")],
        }
    return facts


def render_facts(facts: dict) -> str:
    """graph_facts() for one function, as prompt text. Empty string when there is
    nothing to assert — an empty section invites the model to fill it in."""
    if not facts:
        return ""
    parts: list[str] = []
    if facts.get("callees"):
        parts.append("  resolved callees: " + ", ".join(facts["callees"]))
    if facts.get("externals"):
        parts.append("  external calls: " + ", ".join(
            f"{e['name']} [{e.get('kind') or 'other'}]" for e in facts["externals"]))
    if facts.get("fields"):
        parts.append("  fields: " + ", ".join(
            f"{f['field']}:{f.get('type') or '?'} ({f['access']})" for f in facts["fields"]))
    return "\n".join(parts)


def needs_summary(functions: list[dict]) -> list[dict]:
    """Functions needing (re)summarization: stale CONTENT or stale SHAPE.

    Two independent staleness conditions, and the second is easy to miss:

      * summary_hash != body_hash — the code changed. The whole incremental story.
      * sig_schema_version < SCHEMA_VERSION — the code did not change, but the
        summary predates fields a later pass now reads. This one is dangerous
        precisely because it looks fine: body_hash matches, so a version-blind check
        calls the summary fresh, and every consumer of a new field then reports zero
        findings instead of an error.

    A first run returns everything; a re-run after editing one method returns one
    function; a re-run after a schema bump returns only what lacks the new fields."""
    return [f for f in functions
            if f.get("body_hash")
            and (f.get("summary_hash") != f.get("body_hash")
                 or int(f.get("sig_schema_version") or 0) < contract.SCHEMA_VERSION)]


def write_summaries(store, repo: str, rows: list[dict], model: str) -> int:
    """Persist summaries onto their Function nodes.

    `summary_hash` is set to the body_hash the summary was DERIVED FROM, not the
    current one — so if the body changed between read and write, the next run
    correctly treats the summary as stale instead of trusting it.
    """
    if not rows:
        return 0
    from .priority import derive_signals  # local: avoids a cycle via contract

    payload = [{
        "id": r["id"],
        "summary": json.dumps(r["summary"], separators=(",", ":"), sort_keys=True),
        "hash": r["body_hash"],
        "model": model,
        "ts": time.time(),
        # Scalars projected out of the JSON in the SAME write. Cypher cannot read
        # into a JSON string, so without these no summary fact can take part in an
        # ORDER BY ... LIMIT — which is precisely the query that decides which paths
        # are worth spending a model call on. Written here rather than in a later
        # pass so the projection can never drift from the summary it came from.
        "sig": derive_signals(r["summary"]),
    } for r in rows]
    store._run(
        """
        UNWIND $rows AS row
        MATCH (f:CodeNode {id: row.id})
        SET f.summary_json = row.summary,
            f.summary_hash = row.hash,
            f.summary_model = row.model,
            f.summary_at = row.ts
        SET f += row.sig
        """,
        rows=payload,
    )
    _log.info("[analysis] wrote %s summary(ies) [model=%s]", len(payload), model)
    return len(payload)


def load_summaries(store, function_ids: list[str]) -> dict[str, dict]:
    """Stored summaries by function id, for Pass B's path joins."""
    if not function_ids:
        return {}
    rows = store.read(
        """
        UNWIND $ids AS fid
        MATCH (f:CodeNode {id: fid})
        WHERE f.summary_json IS NOT NULL
        RETURN fid AS id, f.summary_json AS summary
        """,
        ids=function_ids,
    )
    out: dict[str, dict] = {}
    for row in rows:
        try:
            out[row["id"]] = json.loads(row["summary"])
        except (ValueError, TypeError):
            _log.warning("[analysis] unparseable stored summary for %s", row["id"])
    return out


def summary_coverage(store, repo: str) -> dict:
    """How much of the repo currently has a VALID (non-stale) summary.

    Counted by comparing summary_hash to body_hash rather than by presence, so a
    summary left over from an older version of a function is reported as stale
    rather than as coverage."""
    rows = store.read(
        f"""
        MATCH (f:{_FUNCTION_LABEL} {{repo: $repo}})
        WHERE f.body_hash IS NOT NULL
        RETURN count(f) AS total,
               sum(CASE WHEN f.summary_hash = f.body_hash THEN 1 ELSE 0 END) AS fresh,
               sum(CASE WHEN f.summary_json IS NOT NULL
                         AND f.summary_hash <> f.body_hash THEN 1 ELSE 0 END) AS stale
        """,
        repo=repo,
    )
    return dict(rows[0]) if rows else {"total": 0, "fresh": 0, "stale": 0}

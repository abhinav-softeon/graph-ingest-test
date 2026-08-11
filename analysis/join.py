"""Contract mismatches across a CALLS edge. Deterministic — no LLM.

WHAT THIS ANSWERS THAT NOTHING ELSE DOES
javac already proved the TYPES line up on every call in this repo — that is where
the bytecode came from, at 99.98% coverage, so a type mismatch would not have
compiled. What javac never checked is the CONTRACT: a method that returns null on
its not-found branch, and a caller that dereferences the result immediately. Both
sides are legal Java. Only one of them is correct.

WHY IT HAS TO BE A GRAPH JOIN
The callee and the caller are almost always in different files, and the file pass
sees one file at a time. Neither side can see the other. The graph is the only
place the two observations meet, so the file pass records each side as a property
and this module matches them across the CALLS edge that already exists.

LISTS, NOT JSON
Every field joined on here is a scalar or a flat list of strings. Summaries are
stored as a JSON string and Cypher cannot read into one — that limitation is the
whole reason priority.py exists, and a `contracts` block hidden inside the JSON
would be exactly as unqueryable as the rest of it.

THESE ARE CANDIDATES, NOT FINDINGS
A row here says "this callee can return null, and this caller does not appear to
guard it". It does NOT say the null-returning branch is reachable from this
caller, or that the value ever actually arrives null. That is the same split as
taint — the chain exists is not the value flows — and it is why every row goes to
the adversarial pass before it goes to a human. The precision bar on the caller
side can therefore be loose: it is a candidate generator, and something else
decides.
"""
from __future__ import annotations

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Same trust set the rest of the analysis uses. Not optional: name-strategy edges
# sit near 5% precision, and this module turns every edge it accepts into a
# reported defect — so an untrusted edge here does not merely add noise to a
# ranking, it manufactures a finding about a call that was never made.
_TRUSTED = (
    "(r.strategy = 'bytecode' OR r.strategy STARTS WITH 'receiver_type' "
    "OR r.strategy STARTS WITH 'same_')"
)


def _rows(store, query: str, **params) -> list[dict]:
    return [dict(r) for r in store.read(query, **params)]


def unchecked_null(store, repo: str, limit: int = 5000) -> list[dict]:
    """Callee can return null; caller uses the result without checking it.

    `callee.name IN caller.sig_unguarded_calls` is the join. The file pass writes
    `unguarded_calls` as bare method names because that is what it can see in the
    caller's source — it has no way to know which overload or which class the name
    resolves to. The CALLS edge supplies exactly that, which is why the name match
    is sound here and would not be on its own.
    """
    return _rows(
        store,
        f"""
        MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
        WHERE callee.sig_may_return_null = true
          AND caller.sig_unguarded_calls IS NOT NULL
          AND callee.name IN caller.sig_unguarded_calls
          AND {_TRUSTED}
        RETURN caller.id AS caller_id, caller.fqn AS caller_fqn,
               caller.file AS file,
               coalesce(r.line, caller.start_line) AS line,
               callee.id AS callee_id, callee.fqn AS callee_fqn,
               coalesce(callee.sig_null_condition, '') AS condition,
               r.strategy AS strategy
        ORDER BY file, line
        LIMIT $limit
        """,
        repo=repo, limit=limit,
    )


def unchecked_sentinel(store, repo: str, limit: int = 5000) -> list[dict]:
    """Callee signals failure with a non-null sentinel the caller never checks.

    Same shape as unchecked_null and deliberately a separate query rather than an
    OR: a `-1` that nobody checks reads differently in a report, ranks differently,
    and is refuted differently, so merging them would only make both harder to act
    on. Reuses `unguarded_calls` because "used the result without checking it" is
    the same observation regardless of what the failure value is.
    """
    return _rows(
        store,
        f"""
        MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
        WHERE callee.sig_returns_sentinel IS NOT NULL AND callee.sig_returns_sentinel <> ''
          AND caller.sig_unguarded_calls IS NOT NULL
          AND callee.name IN caller.sig_unguarded_calls
          AND {_TRUSTED}
        RETURN caller.id AS caller_id, caller.fqn AS caller_fqn,
               caller.file AS file,
               coalesce(r.line, caller.start_line) AS line,
               callee.id AS callee_id, callee.fqn AS callee_fqn,
               callee.sig_returns_sentinel AS sentinel,
               r.strategy AS strategy
        ORDER BY file, line
        LIMIT $limit
        """,
        repo=repo, limit=limit,
    )


def swallowed_failure(store, repo: str, limit: int = 5000) -> list[dict]:
    """Callee throws to signal failure; caller catches and carries on regardless.

    Distinct from an empty catch block found inside one file: this one needs both
    ends. The single-file pass can see that a catch is empty, but not that the
    call inside it was the operation the whole function existed to perform.
    """
    return _rows(
        store,
        f"""
        MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
        WHERE caller.sig_swallowed_calls IS NOT NULL
          AND callee.name IN caller.sig_swallowed_calls
          AND {_TRUSTED}
        RETURN caller.id AS caller_id, caller.fqn AS caller_fqn,
               caller.file AS file,
               coalesce(r.line, caller.start_line) AS line,
               callee.id AS callee_id, callee.fqn AS callee_fqn,
               r.strategy AS strategy
        ORDER BY file, line
        LIMIT $limit
        """,
        repo=repo, limit=limit,
    )


def leaked_resource_escapes(store, repo: str, limit: int = 5000) -> list[dict]:
    """Callee hands back a resource it did not close, and the caller does not either.

    The leak question that a single file genuinely cannot answer. `db.acquires`
    without `db.released_in_finally` inside one function is already a finding from
    the file pass; this is the other shape — the acquire is in one function, the
    responsibility moves to the caller, and neither end takes it.
    """
    return _rows(
        store,
        f"""
        MATCH (caller:Function {{repo: $repo}})-[r:CALLS]->(callee:Function)
        WHERE callee.sig_acquires = true
          AND coalesce(callee.sig_released_in_finally, false) = false
          AND coalesce(caller.sig_released_in_finally, false) = false
          AND {_TRUSTED}
        RETURN caller.id AS caller_id, caller.fqn AS caller_fqn,
               caller.file AS file,
               coalesce(r.line, caller.start_line) AS line,
               callee.id AS callee_id, callee.fqn AS callee_fqn,
               r.strategy AS strategy
        ORDER BY file, line
        LIMIT $limit
        """,
        repo=repo, limit=limit,
    )


# kind values come from contract.py's findings enum so everything downstream —
# priority.py's ranking, findings.py's dedupe — treats a join candidate and a
# single-file finding identically. A separate vocabulary here would mean two
# things that are the same defect never dedupe against each other.
_QUERIES = (
    ("null_dereference", unchecked_null),
    ("correctness", unchecked_sentinel),
    ("error_handling", swallowed_failure),
    ("resource_leak", leaked_resource_escapes),
)


def all_mismatches(store, repo: str, limit: int = 5000) -> list[dict]:
    """Every contract mismatch, tagged with its kind. Seconds, no model calls.

    Reports a per-query count even when it is zero, because zero has two very
    different causes: nothing is wrong, or the file pass never ran and the
    properties this joins on do not exist. `coverage()` below tells them apart —
    call it before believing an empty result.
    """
    out: list[dict] = []
    for kind, fn in _QUERIES:
        rows = fn(store, repo, limit)
        for row in rows:
            row["kind"] = kind
            out.append(row)
        _log.info("[join] %s: %s candidate(s)", kind, len(rows))
    return out


def coverage(store, repo: str) -> dict:
    """How many functions carry the contract properties this module joins on.

    THE FAILURE THIS EXISTS TO MAKE LOUD: with no file pass, every query above
    matches nothing and `all_mismatches` returns an empty list — which reads
    exactly like a clean codebase. It is the same silent zero as summary_seeds,
    and the fix is the same: report the denominator, not just the numerator.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        RETURN count(f) AS total,
               sum(CASE WHEN f.sig_may_return_null IS NOT NULL THEN 1 ELSE 0 END) AS with_contracts,
               sum(CASE WHEN f.sig_unguarded_calls IS NOT NULL THEN 1 ELSE 0 END) AS with_call_guards
        """,
        repo=repo,
    )
    out = dict(rows[0]) if rows else {}
    total = out.get("total") or 0
    have = out.get("with_contracts") or 0
    out["fraction"] = round(have / total, 4) if total else 0.0
    if not have:
        _log.warning(
            "[join] NO function carries contract properties — the file pass has not "
            "run for repo=%s, so every join below returns 0 candidates. That is an "
            "un-run analysis, not a clean codebase.", repo,
        )
    return out

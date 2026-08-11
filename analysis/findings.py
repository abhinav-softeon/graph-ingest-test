"""Deduplication, ranking, and dismissal memory for the final report.

THE FINGERPRINT IS KEYED ON body_hash, AND THAT IS THE WHOLE DESIGN
A dismissal has to survive re-runs, or reviewers re-triage the same false positive
every time and stop reading the output. But it must NOT survive a change to the code
it was about, or a real regression gets silently suppressed by a stale "not a bug".

Keying the fingerprint on the sink function's `body_hash` gives both properties for
free: identical code re-runs to the same fingerprint and stays dismissed; edited code
produces a new fingerprint and is re-examined. No expiry policy, no TTL, no manual
invalidation.

WHY DEDUP IS NOT JUST TIDINESS
Many entry points reach one vulnerable sink. Reported per-path, one bug becomes
forty rows and the report looks like a catastrophe. Collapsed by fingerprint with the
entry points listed as reach, it reads as what it is: one bug, reachable forty ways —
which is also the number that tells a reviewer how urgent it is.
"""
from __future__ import annotations

import hashlib
import json
import time

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


def fingerprint(store, repo: str, finding: dict) -> str:
    """Stable id for a finding: (kind, sink fqn, sink body_hash).

    Deliberately excludes the entry point and the path — those vary between runs as
    call-graph precision changes, while the bug itself does not. Deliberately
    INCLUDES the sink's body_hash so the id changes the moment the vulnerable code
    changes.
    """
    sink_hash = ""
    ids = finding.get("path_ids") or []
    if ids:
        rows = store.read(
            "MATCH (f:CodeNode {id: $id}) RETURN f.body_hash AS h", id=ids[-1])
        if rows:
            sink_hash = rows[0].get("h") or ""
    key = "\x00".join([
        str(finding.get("kind") or ""),
        str(finding.get("sink") or finding.get("file") or ""),
        sink_hash,
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def dedupe(store, repo: str, findings: list[dict]) -> list[dict]:
    """Collapse findings that describe the same defect, keeping reach as evidence.

    The surviving row is the one with the SHORTEST path — the most direct route to
    the bug is the clearest one to hand a reviewer. Every other entry point is
    retained under `also_reachable_from`, so nothing is lost and the fan-in is
    visible.
    """
    groups: dict[str, list[dict]] = {}
    for f in findings:
        fp = f.get("fingerprint") or fingerprint(store, repo, f)
        f["fingerprint"] = fp
        groups.setdefault(fp, []).append(f)

    out = []
    for fp, group in groups.items():
        group.sort(key=lambda f: len(f.get("path_ids") or []))
        primary = dict(group[0])
        entries = sorted({f.get("entry") for f in group if f.get("entry")})
        primary["reachable_from_count"] = len(entries)
        primary["also_reachable_from"] = [e for e in entries
                                          if e != primary.get("entry")][:20]
        primary["duplicate_paths"] = len(group) - 1
        out.append(primary)

    if len(out) < len(findings):
        _log.info("[findings] deduped %s -> %s distinct defect(s)", len(findings), len(out))
    return out


def rank(findings: list[dict]) -> list[dict]:
    """Order for human attention.

    Severity dominates, then how many entry points reach it (blast radius), then a
    short path — because a two-frame chain is easier to confirm and fix than an
    eight-frame one, and getting an easy confirmation early builds the trust that
    makes the rest of the report get read.
    """
    def key(f: dict):
        return (
            _SEVERITY_ORDER.get(str(f.get("severity") or "none").lower(), 4),
            -(f.get("reachable_from_count") or 0),
            len(f.get("path_ids") or []),
            str(f.get("file") or ""),
            f.get("line") or 0,
        )
    return sorted(findings, key=key)


def load_dismissals(store, repo: str) -> dict[str, dict]:
    """Previously dismissed fingerprints, so they are not re-reported.

    Stored on a per-repo GraphMeta-style node rather than on Function nodes: a
    dismissal outlives the function it was about (that is the point of keying on
    body_hash), so attaching it to the node would lose it on re-index.
    """
    rows = store.read(
        """
        MATCH (d:AnalysisDismissal {repo: $repo})
        RETURN d.fingerprint AS fingerprint, d.reason AS reason,
               d.by AS by, d.at AS at
        """,
        repo=repo,
    )
    return {r["fingerprint"]: dict(r) for r in rows}


def save_dismissals(store, repo: str, findings: list[dict], by: str = "adversarial_pass") -> int:
    """Persist dismissals with their reason.

    The reason is stored, not just the fact: a bare suppression list is unauditable,
    and the next reviewer needs to know WHY something was ruled out to judge whether
    the ruling still applies.
    """
    rows = [{
        "fingerprint": f["fingerprint"],
        "reason": (f.get("dismissed_because")
                   or (f.get("adjudication") or {}).get("votes") and
                   json.dumps((f.get("adjudication") or {}).get("votes"))[:600]
                   or "dismissed"),
        "by": by,
        "at": time.time(),
    } for f in findings if f.get("dismissed") and f.get("fingerprint")]
    if not rows:
        return 0
    store._run(
        """
        UNWIND $rows AS row
        MERGE (d:AnalysisDismissal {repo: $repo, fingerprint: row.fingerprint})
        SET d.reason = row.reason, d.by = row.by, d.at = row.at
        """,
        repo=repo, rows=rows,
    )
    _log.info("[findings] recorded %s dismissal(s)", len(rows))
    return len(rows)


def apply_dismissals(findings: list[dict], dismissals: dict[str, dict]) -> tuple[list, list]:
    """Split into (to report, previously dismissed).

    Applied AFTER fingerprinting and BEFORE ranking, so a finding dismissed in an
    earlier run never reaches the report — and never costs Pass D another panel."""
    keep, suppressed = [], []
    for f in findings:
        prior = dismissals.get(f.get("fingerprint") or "")
        if prior:
            f["previously_dismissed"] = prior
            suppressed.append(f)
        else:
            keep.append(f)
    if suppressed:
        _log.info("[findings] suppressed %s finding(s) dismissed in an earlier run",
                  len(suppressed))
    return keep, suppressed


def report(store, repo: str, confirmed: list[dict], killed: list[dict],
           persist: bool = True) -> dict:
    """Final report: dedupe, apply prior dismissals, rank, optionally persist.

    ``persist`` writes this run's dismissals so the next run skips them. Turn it off
    while tuning prompts — otherwise an early bad kill becomes permanent and you
    stop seeing the finding you were trying to fix.
    """
    # A verdict the model declined to classify is not a finding. One reached a real
    # report as kind='none' severity='none' — it survived adjudication precisely
    # because there was nothing specific to refute, which is the wrong reason to
    # publish something. Dropped here rather than at each producer, since every one
    # of them can emit it and they all converge on this function.
    confirmed = [f for f in confirmed
                 if (f.get("kind") or "none") != "none"
                 and (f.get("severity") or "none") != "none"]

    deduped = dedupe(store, repo, confirmed)
    for f in killed:
        f.setdefault("fingerprint", fingerprint(store, repo, f))

    prior = load_dismissals(store, repo)
    keep, suppressed = apply_dismissals(deduped, prior)
    ranked = rank(keep)

    saved = save_dismissals(store, repo, killed) if persist else 0

    by_severity: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for f in ranked:
        by_severity[str(f.get("severity") or "none")] = by_severity.get(
            str(f.get("severity") or "none"), 0) + 1
        by_kind[str(f.get("kind") or "other")] = by_kind.get(
            str(f.get("kind") or "other"), 0) + 1

    out = {
        "findings": ranked,
        "counts": {
            "reported": len(ranked),
            "deduped_away": len(confirmed) - len(deduped),
            "suppressed_by_prior_dismissal": len(suppressed),
            "killed_this_run": len(killed),
            "dismissals_recorded": saved,
            "by_severity": by_severity,
            "by_kind": by_kind,
        },
    }
    _log.info("[findings] report: %s", out["counts"])
    return out

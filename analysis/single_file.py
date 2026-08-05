"""Findings Pass A can decide alone, reported without the path pipeline.

WHY THIS EXISTS
Pass A already emits `findings[]` — "defects visible in THIS body alone" — on every
call, for every function, and it was being stored and ignored. The only consumer was
priority.derive_signals, which counted them toward a risk score and discarded the
content. Every defect they described was then handed to path enumeration, Pass B,
Pass C and Pass D to be rediscovered.

THE MISMATCH THAT COST RECALL, MEASURED
A resource leak is a property of ONE function. The acquire, the missing finally, and
the throwing call in between are all inside a single body — Pass A answers it
definitively from the file it already read. Routing it afterwards through machinery
built for taint (multi-frame paths, summary joins, a lens panel whose premise is data
travelling between functions) adds no information and four opportunities to lose it.
On the corpus that showed as 15/15 correct at Pass A and 8/15 in the report: 4 killed
by the adversarial panel, 3 never reaching Pass B.

WHAT BELONGS HERE AND WHAT DOES NOT
Only kinds decidable from one body. `sql_injection` deliberately does NOT qualify: a
single file shows the concatenation but cannot show whether the value is
attacker-controlled, and that is exactly the question the path pipeline exists to
answer. Reporting injections from here would trade the sanitizer detection — which
scores 100% precision through the paths — for recall that is not worth it.
"""
from __future__ import annotations

import json

from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

# Decidable from one function body. Everything else needs callers or callees:
# 'sql_injection' needs to know whether the value is attacker-controlled;
# 'path_traversal' and 'deserialization' likewise depend on where input came from.
SINGLE_FILE_KINDS = frozenset({
    "resource_leak", "error_handling", "concurrency", "correctness",
})

# Low-confidence findings are kept but capped at severity 'low' (see _severity).
# They were excluded entirely before, which is why the report never contained a
# single 'low' row — and improvements/optimizations are exactly what low is for.
_MIN_CONFIDENCE = {"high", "medium", "low"}

# Kinds that can never exceed a given level from a SINGLE BODY, regardless of what
# the model says. Not a severity table — a ceiling.
#
# The distinction matters: severity is meant to be picked by CERTAINTY (see
# contract._PATH_VERDICT), and certainty about impact is exactly what one function
# body cannot establish. A leak here is real but its blast radius depends on callers
# this pass cannot see, so it cannot be 'critical'. The previous version assigned a
# flat severity per kind, which made severity a relabelling of kind — measured on the
# corpus as 33/33 injections 'critical' and 69/71 leaks 'high', with 'low' never once
# emitted.
_MAX_SEVERITY_BY_KIND = {
    "resource_leak": "high",     # certain leak, conditional impact
    "concurrency": "high",
    "correctness": "high",
    "error_handling": "medium",  # rarely demonstrable from one body
}

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


def _severity(kind: str, confidence: str) -> str:
    """Model-reported severity, lowered to what a single body can actually support.

    Two independent caps, and the lower wins:
      * the kind ceiling above — impact this pass cannot see past;
      * confidence — a 'low'-confidence observation is by definition speculative,
        which the rubric defines as at most 'medium', and improvements land on 'low'.
    """
    ceiling = _MAX_SEVERITY_BY_KIND.get(kind, "medium")
    by_conf = {"high": "high", "medium": "medium", "low": "low"}.get(
        str(confidence or "").lower(), "low")
    return max(ceiling, by_conf, key=lambda s: _SEVERITY_RANK[s])


def harvest(store, repo: str, kinds: frozenset[str] = SINGLE_FILE_KINDS) -> list[dict]:
    """Pass A's own findings, shaped like path findings so the report can merge them.

    Costs nothing — the summaries are already stored. This is reading work that was
    already paid for and previously discarded.
    """
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.summary_json IS NOT NULL AND f.summary_hash = f.body_hash
        RETURN f.id AS id, f.fqn AS fqn, f.file AS file,
               f.start_line AS start_line, f.summary_json AS summary
        """,
        repo=repo,
    )
    out: list[dict] = []
    for row in rows:
        try:
            summary = json.loads(row["summary"])
        except (ValueError, TypeError):
            continue
        for finding in summary.get("findings") or []:
            kind = finding.get("kind")
            if kind not in kinds:
                continue
            if str(finding.get("confidence") or "").lower() not in _MIN_CONFIDENCE:
                continue
            out.append({
                "kind": kind,
                "severity": _severity(kind, finding.get("confidence")),
                # The vulnerable function is both ends: there is no chain, and
                # pretending otherwise would make the report's path column a lie.
                "entry": row["fqn"],
                "sink": row["fqn"],
                "file": row["file"],
                "line": finding.get("line") or row["start_line"],
                "path_ids": [row["id"]],
                "path_fqns": [row["fqn"]],
                "hops": 0,
                "exploitable": False,
                "is_defect": True,
                "reasoning": finding.get("detail") or "",
                "evidence": [{"function": row["fqn"],
                              "line": finding.get("line") or row["start_line"],
                              "what": finding.get("detail") or ""}],
                "confidence": finding.get("confidence"),
                "source": "pass_a",
                "need_source_for": [],
            })
    by_kind: dict[str, int] = {}
    for f in out:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    _log.info("[single_file] harvested %s finding(s) from stored summaries: %s",
              len(out), by_kind or "none")
    return out

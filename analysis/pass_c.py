"""Pass C — targeted expansion. Fetch real source only where Pass B got stuck.

THIS IS THE ONLY PASS THAT IS GENUINELY AGENTIC, AND THAT IS THE POINT
Pass A and Pass B are batch jobs: uniform work, fixed shape, parallel, cacheable.
An agent adds nothing there but orchestration overhead. Pass C is different — what
it fetches depends on what the previous step discovered, so the control flow cannot
be written in advance. That is the actual criterion for reaching for an agent, not
"this task is important".

WHY IT IS CHEAP DESPITE SENDING SOURCE
It runs only on verdicts that named something in `need_source_for`, and it fetches
only those named functions. Most paths resolve in Pass B from summaries alone; the
residue is small, and it is exactly the residue where a summary was insufficient.
Sending source for those and only those is what keeps precision high without paying
source-level cost everywhere.

THE RE-READ IS DELIBERATE AND BOUNDED
This is the second time the model sees these function bodies (Pass A was the
first) — the "no more than twice, and only for a reason" case. The reason is
recorded on every expansion (`asked_for`), and depth is capped so a chain of "now I
need to see this one too" cannot walk the whole repo.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, contract
from .llm import get_client
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

MAX_EXPANSIONS_PER_FINDING = 6   # bodies fetched for one finding
MAX_ROUNDS = 2                   # "I need one more" hops before giving up

SYSTEM = """\
You are re-examining a suspected vulnerability with the ACTUAL SOURCE of the
functions that were previously unclear.

You were given structured summaries before and said you needed to see specific
function bodies to decide. Those bodies follow. Decide now.

Read the real code, not the summary. If the source contradicts what a summary
claimed, the source wins and you should say so in your reasoning — a summary is a
reading of the code, the code is the code.

Decide BOTH flags on the evidence in front of you. They are independent:

`exploitable` — a TAINT question only:
- Untrusted data actually reaches the dangerous operation unaltered -> true.
- Any frame validates, escapes, parameterizes, or replaces the value -> false, and
  name that frame in `sanitized_at`.

`is_defect` — is this a real bug at all, attacker-triggerable or not:
- For a resource leak: is the release reached on EVERY path out of the function,
  including when an intermediate call throws? If not it leaks, and that is
  `is_defect: true` with `exploitable: false` — no attacker is involved and it will
  still exhaust the pool in production.
- Every exploitable path is also a defect.
- Both false means the code is genuinely fine.

If a body you now have reveals that you need ONE more function to be sure, name it
in `need_source_for`. Do not name functions speculatively — each one costs another
round, and rounds are capped. If you still cannot tell after this, answer false and
explain what remains unknown."""


@dataclass
class PassCReport:
    findings_in: int = 0
    findings_resolved: int = 0
    findings_still_unknown: int = 0
    confirmed: int = 0
    refuted: int = 0
    bodies_fetched: int = 0
    rounds_used: int = 0
    calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "findings_in": self.findings_in,
            "findings_resolved": self.findings_resolved,
            "findings_still_unknown": self.findings_still_unknown,
            "confirmed": self.confirmed,
            "refuted": self.refuted,
            "bodies_fetched": self.bodies_fetched,
            "rounds_used": self.rounds_used,
            "calls_made": self.calls_made,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "errors": self.errors[:20],
            "seconds": round(self.seconds, 1),
        }


def resolve_names(store, repo: str, names: list[str]) -> list[dict]:
    """Map names the model asked for onto real Function nodes.

    Matched on fqn tail and bare name, because the model writes what it saw in the
    path listing (`UserDao#findById`, `findById`, `dao.findById`) rather than a node
    id. Ambiguity is capped rather than resolved by guessing: if a bare name matches
    many functions, the first few are returned and the rest ignored, since sending
    twenty same-named bodies would drown the signal it was asked for.
    """
    if not names:
        return []
    wanted = []
    for raw in names:
        tail = str(raw).replace("()", "").strip()
        tail = tail.rsplit("#", 1)[-1].rsplit(".", 1)[-1].split("(", 1)[0].strip()
        if tail:
            wanted.append(tail)
    if not wanted:
        return []
    rows = store.read(
        """
        MATCH (f:Function {repo: $repo})
        WHERE f.name IN $names AND f.file IS NOT NULL
        RETURN f.id AS id, f.fqn AS fqn, f.name AS name, f.file AS file,
               f.start_line AS start_line, f.end_line AS end_line
        ORDER BY f.fqn
        LIMIT 40
        """,
        repo=repo, names=sorted(set(wanted)),
    )
    return [dict(r) for r in rows]


def read_body(root: str, relpath: str, start: int, end: int) -> str | None:
    """Exact source span for a function. Positions come from tree-sitter and are
    exact, so this slices precisely rather than guessing at boundaries."""
    path = os.path.join(root, (relpath or "").replace("/", os.sep))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        _log.warning("[pass_c] unreadable %s: %s", relpath, exc)
        return None
    s = max(1, int(start or 1))
    e = min(len(lines), int(end or s))
    return "".join(f"{i:>5} | {lines[i - 1]}" for i in range(s, e + 1))


def _expand_one(client, store, repo: str, root: str, finding: dict) -> dict:
    """Fetch what one finding asked for and re-judge, up to MAX_ROUNDS."""
    asked: list[str] = list(finding.get("need_source_for") or [])
    fetched_ids: set[str] = set()
    bodies: list[str] = []
    results = []
    rounds = 0
    verdict = None

    while rounds < MAX_ROUNDS and asked:
        rounds += 1
        targets = [t for t in resolve_names(store, repo, asked)
                   if t["id"] not in fetched_ids][:MAX_EXPANSIONS_PER_FINDING - len(fetched_ids)]
        if not targets:
            break
        for t in targets:
            body = read_body(root, t["file"], t["start_line"], t["end_line"])
            if body is None:
                continue
            fetched_ids.add(t["id"])
            bodies.append(f"### {t['fqn']}   ({t['file']}:{t['start_line']})\n{body}")
        if not bodies:
            break

        user = _build_user(finding, bodies, asked)
        res = client.complete(SYSTEM, user, schema=contract.PATH_VERDICT_SCHEMA)
        results.append(res)
        if not res.ok or res.parsed is None:
            _log.warning("[pass_c] expansion call failed: %s", res.error or "no JSON")
            break
        try:
            verdicts = contract.validate_verdicts(res.parsed, 1)
        except contract.ValidationError as exc:
            _log.warning("[pass_c] expansion failed validation: %s", exc)
            break
        if not verdicts:
            break
        verdict = verdicts[0]
        asked = [n for n in (verdict.get("need_source_for") or [])
                 if n not in (finding.get("need_source_for") or [])]
        if not asked:
            break

    return {"verdict": verdict, "results": results, "rounds": rounds,
            "bodies": len(fetched_ids), "asked_for": list(finding.get("need_source_for") or [])}


def _build_user(finding: dict, bodies: list[str], asked: list[str]) -> str:
    chain = " -> ".join(finding.get("path_fqns") or [])
    return f"""\
Suspected {finding.get('kind') or 'issue'} — re-examine with real source.

PATH (index 0 for your verdict):
{chain}

Dangerous operation: {', '.join(finding.get('sink_kinds') or []) or 'unknown'}
at {finding.get('file')}:{finding.get('line')}

Earlier reasoning from summaries alone:
{finding.get('reasoning') or '(none)'}

You asked to see: {', '.join(asked)}

ACTUAL SOURCE:

{chr(10).join(bodies)}

Return exactly one verdict with path_index 0."""


def run_pass_c(store, repo: str, root: str, findings: list[dict],
               model: str | None = None) -> PassCReport:
    """Expand and re-judge only the findings that asked for source.

    Findings with an empty `need_source_for` pass through untouched — Pass B already
    decided them from summaries and re-litigating would just cost tokens.
    """
    model = model or config.adjudicator_model()
    rep = PassCReport()
    t0 = time.monotonic()

    needs = [f for f in findings if f.get("need_source_for")]
    passthrough = [f for f in findings if not f.get("need_source_for")]
    rep.findings_in = len(needs)
    rep.findings.extend(passthrough)
    if not needs:
        _log.info("[pass_c] nothing to expand — no finding requested source")
        rep.seconds = time.monotonic() - t0
        return rep

    # Constructed only once there is work: no expansion requested means no model is
    # needed, and eagerly building one would fail when the SDK is absent.
    client = get_client(model, pass_name="pass_c")
    _log.info("[pass_c] expanding %s finding(s) of %s; model=%s",
              len(needs), len(findings), model)

    with ThreadPoolExecutor(max_workers=config.llm_workers()) as pool:
        futures = {pool.submit(_expand_one, client, store, repo, root, f): f
                   for f in needs}
        for fut in as_completed(futures):
            finding = futures[fut]
            try:
                got = fut.result()
            except Exception as exc:  # noqa: BLE001
                _log.warning("[pass_c] expansion raised: %s", exc)
                rep.errors.append(str(exc))
                rep.findings_still_unknown += 1
                continue
            rep.calls_made += len(got["results"])
            rep.bodies_fetched += got["bodies"]
            rep.rounds_used += got["rounds"]
            for res in got["results"]:
                rep.input_tokens += res.input_tokens
                rep.output_tokens += res.output_tokens

            verdict = got["verdict"]
            if verdict is None:
                rep.findings_still_unknown += 1
                # Kept, but marked: an unresolved expansion is not evidence of
                # innocence, and dropping it silently would hide a coverage gap.
                finding["expansion"] = {"resolved": False,
                                        "asked_for": got["asked_for"]}
                rep.findings.append(finding)
                continue

            rep.findings_resolved += 1
            enriched = dict(finding)
            enriched.update({
                "kind": verdict.get("kind") or finding.get("kind"),
                "severity": verdict.get("severity") or finding.get("severity"),
                "reasoning": verdict.get("reasoning") or finding.get("reasoning"),
                "evidence": verdict.get("evidence") or finding.get("evidence"),
                "sanitized_at": verdict.get("sanitized_at") or "",
                "expansion": {"resolved": True, "asked_for": got["asked_for"],
                              "bodies_read": got["bodies"], "rounds": got["rounds"]},
            })
            # Same two-flag rule as Pass B, for the same reason: gating on
            # `exploitable` alone dismisses every resource leak, because a leak is a
            # defect and never a taint exploit. Re-judging with real source in hand
            # and THEN throwing the answer away on a taint test would waste the most
            # expensive verdict in the pipeline.
            enriched["exploitable"] = bool(verdict.get("exploitable"))
            enriched["is_defect"] = bool(verdict.get("is_defect"))
            if verdict.get("exploitable") or verdict.get("is_defect"):
                rep.confirmed += 1
                rep.findings.append(enriched)
            else:
                # Refuted with real source in hand — the most reliable dismissal
                # this pipeline produces, and worth keeping for the record.
                rep.refuted += 1
                enriched["dismissed"] = True
                rep.findings.append(enriched)

    rep.seconds = time.monotonic() - t0
    _log.info("[pass_c] done: %s", rep.summary())
    return rep

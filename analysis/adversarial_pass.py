"""Pass D — adversarial adjudication. Several independent verifiers try to REFUTE.

WHY THIS IS THE HIGHEST-VALUE USE OF AN UNLIMITED BUDGET
Discovery precision is mediocre; triage precision is high. Asking "is this real?"
of a specific, fully-stated claim is a far easier question than "find the bugs", and
it is the step that decides whether anyone trusts the output. This is also the piece
SonarQube structurally cannot do and CodeRabbit has no whole-repo context for.

THE PROMPTS ASK FOR REFUTATION, NOT CONFIRMATION
A verifier asked to "check this finding" agrees with it — the claim is right there,
stated confidently, and agreement is the path of least resistance. A verifier asked
to REFUTE it has to actively find the reason it fails. The schema field is `refuted`
and the instruction is "default to refuted when unsure", so uncertainty counts
AGAINST the finding. That asymmetry is the whole mechanism.

DIVERSE LENSES, NOT N IDENTICAL SKEPTICS
Running the same prompt three times mostly reproduces the same blind spot. Each
verifier here gets a different angle — is the input actually attacker-controlled, is
it already neutralized upstream, does the path even execute — so they fail
differently and the panel covers what redundancy cannot.

MAJORITY REFUTE KILLS
A finding survives only if fewer than half its verifiers refute it. Ties go against
the finding, because the cost of a false positive is a reviewer who stops reading.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, contract
from .llm import get_client
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)

_BASE = """\
You are reviewing a reported DEFECT with one job: determine whether it is WRONG.
Assume the reporter may have made a mistake. Look for the specific reason this
finding does not hold.

Not every defect is a vulnerability. A resource leaked on an exception path is a
real bug that no attacker triggers. Judge the finding on its own claim — refuting a
leak because "no attacker controls it" is answering a question nobody asked.

Set `refuted` to true if the finding fails for any reason. Set it to false ONLY if
you actively verified the claim holds and can say why.

If the evidence is insufficient to establish the finding, that is `refuted: true` —
an unproven finding is not a finding. Do not give it the benefit of the doubt."""

# One lens per verifier. They fail in different ways on purpose.
LENSES = {
    "attacker_control": _BASE + """

YOUR LENS: is the data actually attacker-controlled?
Trace the value back to its origin. A "vulnerability" fed by a constant, an
internal identifier, a server-generated value, a config setting, or a value from
another trusted service is not exploitable no matter what it reaches. Ask whether an
external caller can actually influence this value, and refute if they cannot.""",

    "sanitization": _BASE + """

YOUR LENS: is the value already neutralized?
Look for validation, escaping, parameterized queries, type coercion, allow-listing,
or a bound that makes the value harmless before it reaches the operation. A cast to
an integer defeats SQL injection. A parameterized placeholder defeats it regardless
of what the value contains. Refute if anything on the path renders the value safe,
and name it.""",

    "reachability": _BASE + """

YOUR LENS: does this path actually execute?
Consider whether the chain is real: dead code, an unreachable branch, a guard that
returns early, an interface method with no implementation that is ever constructed,
a caller that only runs in tests. Also consider whether the reported call sequence
could be an artifact of imprecise call-graph resolution rather than a real chain.
Refute if the path cannot execute as described in production.""",

    "leak_control_flow": _BASE + """

YOUR LENS: for a resource leak, is the release genuinely skippable?
The claim is that a resource is not released on every path. Check it properly: a
finally block, try-with-resources, a wrapper that closes in its own finally, or a
pooled resource whose close is a no-op all defeat the finding. Conversely, a close()
on the happy path only is a genuine leak. Refute if release is in fact guaranteed on
every path, including exceptional ones.""",

    "resource_ownership": _BASE + """

YOUR LENS: is this function even responsible for releasing the resource?
A function that RECEIVES an already-open resource as a parameter, or returns it to
its caller, is not leaking — the owner is whoever opened it, and closing it here
would be the bug. Refute if this frame does not own the resource's lifetime, if a
caller demonstrably closes it, or if the object is a pooled handle whose close is
returning it rather than destroying it. Do NOT refute merely because no attacker is
involved: a leak is a defect, not an exploit, and attacker control is irrelevant.""",
}

# Lens sets by finding kind. `attacker_control` is deliberately ABSENT from the leak
# set: a resource leak involves no attacker-controlled data by definition, so that
# lens refutes every leak on a technicality that is true and irrelevant. It was in
# this list, and combined with the majority-refute rule it gave leaks a standing
# vote against them before anyone looked at the control flow.
_TAINT_LENSES = ["attacker_control", "sanitization", "reachability"]

# One call carrying every lens, instead of one call per lens. The separate-call design
# existed to keep verifiers independent — but with a single model available for both
# finding and refuting, that independence was already notional, and it cost 3x. The
# prompt below compensates by demanding a verdict per lens BEFORE any conclusion, so
# the lenses are still answered separately even though they share a context.
# GRAPH_PASS_D_SINGLE_CALL=0 restores the fan-out.
def _single_call() -> bool:
    import os
    return os.environ.get("GRAPH_PASS_D_SINGLE_CALL", "1").strip().lower() not in (
        "0", "false", "no", "off")
_LEAK_LENSES = ["leak_control_flow", "resource_ownership", "reachability"]


@dataclass
class PassDReport:
    findings_in: int = 0
    confirmed: int = 0
    killed: int = 0
    votes_cast: int = 0
    calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    confirmed_findings: list = field(default_factory=list)
    killed_findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "findings_in": self.findings_in,
            "confirmed": self.confirmed,
            "killed": self.killed,
            "kill_rate": round(self.killed / self.findings_in, 3) if self.findings_in else 0.0,
            "votes_cast": self.votes_cast,
            "calls_made": self.calls_made,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "errors": self.errors[:20],
            "seconds": round(self.seconds, 1),
        }


def _lenses_for(finding: dict) -> list[str]:
    return _LEAK_LENSES if finding.get("kind") == "resource_leak" else _TAINT_LENSES


def _describe(finding: dict) -> str:
    ev = "\n".join(
        f"  - {e.get('function')}:{e.get('line')} — {e.get('what')}"
        for e in (finding.get("evidence") or [])
    ) or "  (none provided)"
    chain = " -> ".join(finding.get("path_fqns") or []) or "(no path recorded)"
    expansion = finding.get("expansion") or {}
    read_note = ""
    if expansion.get("resolved"):
        read_note = (f"\nNote: the real source of {expansion.get('bodies_read', 0)} "
                     f"function(s) was read before this claim was made.")
    return f"""\
REPORTED FINDING
  kind:     {finding.get('kind')}
  severity: {finding.get('severity')}
  location: {finding.get('file')}:{finding.get('line')}
  sink:     {finding.get('sink')}  [{', '.join(finding.get('sink_kinds') or []) or 'n/a'}]

CALL CHAIN
{chain}

REPORTER'S REASONING
{finding.get('reasoning') or '(none)'}

CITED EVIDENCE
{ev}{read_note}

Sanitizer the reporter believed absent: {finding.get('sanitized_at') or '(none named)'}"""


def _vote(client, finding: dict, lens: str) -> tuple[str, dict | None, object]:
    res = client.complete(LENSES[lens], _describe(finding),
                          schema=contract.REFUTE_SCHEMA)
    if not res.ok or res.parsed is None:
        return lens, None, res
    parsed = res.parsed
    if not isinstance(parsed.get("refuted"), bool):
        return lens, None, res
    return lens, parsed, res


def _vote_all(client, finding: dict, lenses: list[str]):
    """Every lens in one request. Returns [(lens, verdict)], plus the raw result.

    The lens prompts are concatenated under a shared instruction to answer each one
    before concluding. That is weaker than genuinely independent calls — a lens can
    be anchored by the one above it — but with a single model serving as both finder
    and refuter the independence was already nominal, and this costs a third as much.
    """
    body = "\n\n".join(f"=== LENS {i + 1}: {name} ===\n{LENSES[name]}"
                       for i, name in enumerate(lenses))
    system = (
        "You are adjudicating a reported defect through several INDEPENDENT lenses.\n"
        "Answer each lens on its own evidence and state its verdict before moving to\n"
        "the next. Do not let an earlier lens decide a later one — they are meant to\n"
        "fail differently, and a lens that just echoes the previous verdict is a\n"
        "wasted check.\n\n" + body
    )
    res = client.complete(system, _describe(finding), schema=contract.MULTI_REFUTE_SCHEMA)
    if not res.ok or not isinstance(res.parsed, dict):
        return [], res
    out = []
    for i, entry in enumerate(res.parsed.get("lenses") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("refuted"), bool):
            continue
        # Trust position over the echoed name: a model that renames a lens still
        # answered them in order, and dropping the vote would shrink the panel
        # silently, which changes the majority threshold.
        name = lenses[i] if i < len(lenses) else str(entry.get("lens") or f"lens{i}")
        out.append((name, entry))
    return out, res


def run_adversarial_pass(findings: list[dict], model: str | None = None,
               kill_on_tie: bool = True) -> PassDReport:
    """Adjudicate. A finding survives only if a majority of lenses fail to refute it.

    ``kill_on_tie`` decides 2-2 style splits. Default True: a finding half the
    panel can refute is not something to put in front of a reviewer.
    """
    model = model or config.adjudicator_model()
    rep = PassDReport()
    rep.findings_in = len(findings)
    t0 = time.monotonic()

    live = [f for f in findings if not f.get("dismissed")]
    already_dismissed = [f for f in findings if f.get("dismissed")]
    rep.killed += len(already_dismissed)
    rep.killed_findings.extend(already_dismissed)

    # The client is constructed only once there is something to ask it. Nothing
    # live means no panel is needed at all — and building a client for zero work
    # would also fail outright when the provider SDK is not installed, turning a
    # no-op into a crash.
    if not live:
        rep.seconds = time.monotonic() - t0
        _log.info("[adversarial_pass] nothing to adjudicate (%s finding(s), all already "
                  "dismissed)", len(findings))
        return rep

    client = get_client(model, pass_name="adversarial_pass")
    _log.info("[adversarial_pass] adjudicating %s finding(s) (%s already dismissed in Pass C); "
              "model=%s", len(live), len(already_dismissed), model)

    votes: dict[int, list[tuple[str, dict]]] = {id(f): [] for f in live}
    single = _single_call()

    with ThreadPoolExecutor(max_workers=config.llm_workers()) as pool:
        if single:
            futures = {pool.submit(_vote_all, client, f, _lenses_for(f)): f
                       for f in live}
        else:
            futures = {pool.submit(_vote, client, f, lens): f
                       for f in live for lens in _lenses_for(f)}
        for fut in as_completed(futures):
            finding = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                _log.warning("[adversarial_pass] vote raised: %s", exc)
                rep.errors.append(str(exc))
                continue
            if single:
                cast, res = result
            else:
                lens, verdict, res = result
                cast = [(lens, verdict)] if verdict is not None else []
            rep.calls_made += 1
            rep.input_tokens += getattr(res, "input_tokens", 0)
            rep.output_tokens += getattr(res, "output_tokens", 0)
            rep.votes_cast += len(cast)
            votes[id(finding)].extend(cast)

    for finding in live:
        cast = votes[id(finding)]
        if not cast:
            # Every verifier failed. Not evidence either way, so the finding is
            # kept and flagged rather than silently promoted or dropped.
            finding["adjudication"] = {"votes": [], "unverified": True}
            rep.confirmed += 1
            rep.confirmed_findings.append(finding)
            continue
        refutes = sum(1 for _lens, v in cast if v.get("refuted"))
        total = len(cast)
        killed = refutes > total / 2 or (kill_on_tie and refutes * 2 == total)
        finding["adjudication"] = {
            "votes": [{"lens": lens, "refuted": v.get("refuted"),
                       "confidence": v.get("confidence"), "reason": v.get("reason")}
                      for lens, v in cast],
            "refutes": refutes, "total": total,
        }
        if killed:
            finding["dismissed"] = True
            finding["dismissed_because"] = "; ".join(
                v.get("reason", "") for _lens, v in cast if v.get("refuted"))[:600]
            rep.killed += 1
            rep.killed_findings.append(finding)
        else:
            rep.confirmed += 1
            rep.confirmed_findings.append(finding)

    rep.seconds = time.monotonic() - t0
    _log.info("[adversarial_pass] done: %s", rep.summary())
    return rep

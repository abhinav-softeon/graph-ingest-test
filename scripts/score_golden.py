"""Score a saved analysis run against golden.json — what SHOULD it say vs what it did.

Reads artifacts, runs nothing. So re-scoring after changing how a verdict is counted
costs nothing and cannot quietly change the run it is judging, which matters because
the temptation when a number disappoints is to adjust the scorer.

THREE OUTCOMES, NOT TWO
A finding is credited when the vulnerable function appears anywhere on the reported
path — the frame that holds the bug is what matters, not which entry point happened
to reach it. But a third bucket is needed and it is not padding:

  scope_disagreement — the run flagged a DAO whose CONNECTION is correctly closed,
                       citing the Statement/ResultSet that no shape closes. Every
                       DAO in this corpus leaks those, so this is a difference of
                       scope, not an error. Counting it as a false positive would
                       understate precision for being right about something the
                       ground truth deliberately does not measure.

See make_golden.py for why that ambiguity exists and how it was pinned down.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _prf(tp: int, fp: int, fn: int) -> dict:
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "recall": round(tp / (tp + fn), 3) if (tp + fn) else None,
        "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
    }


def _flagged(findings: list[dict], kinds: set[str]) -> dict[str, dict]:
    """Vulnerable functions named by findings of these kinds, best finding kept."""
    out: dict[str, dict] = {}
    for f in findings:
        if f.get("kind") not in kinds:
            continue
        names = [n for n in (f.get("path_fqns") or []) if n]
        if f.get("sink"):
            names.append(f["sink"])
        for name in names:
            out.setdefault(name, f)
    return out


def score(golden: dict, result: dict) -> dict:
    findings = result.get("report", {}).get("findings", []) or []
    daos = {d["fqn"]: d for d in golden["daos"]}

    truth_leak = {d["fqn"] for d in golden["daos"] if d["expect_leak"]}
    truth_inject = {d["fqn"] for d in golden["daos"] if d["expect_injection"]}
    invisible = set(golden["expected_leaks_graph_cannot_see"])
    sanitized = set(golden["must_not_report_injection"])

    leak_flags = _flagged(findings, {"resource_leak"})
    inj_flags = _flagged(findings, {"sql_injection"})

    # Leaks, with the scope split applied.
    leak_hit = {f for f in leak_flags if f in truth_leak}
    leak_wrong, leak_scope = set(), set()
    for fqn in leak_flags:
        if fqn in truth_leak or fqn not in daos:
            continue
        # Connection is closed on every path, but ps/rs never are — anywhere in this
        # corpus. Flagging it is defensible; scoring it as a miss is not.
        (leak_scope if daos[fqn]["stmt_rs_unclosed"] else leak_wrong).add(fqn)

    inj_hit = {f for f in inj_flags if f in truth_inject}
    inj_wrong = {f for f in inj_flags if f in daos and f not in truth_inject}

    return {
        "leaks": _prf(len(leak_hit), len(leak_wrong), len(truth_leak - set(leak_flags))),
        "leaks_detail": {
            "expected": len(truth_leak),
            "found": sorted(leak_hit),
            "missed": sorted(truth_leak - set(leak_flags)),
            "scope_disagreement": sorted(leak_scope),
            "false_positive": sorted(leak_wrong),
        },
        "graph_invisible_leaks": {
            "total": len(invisible),
            "found": len(leak_hit & invisible),
            "recall": round(len(leak_hit & invisible) / len(invisible), 3) if invisible else None,
            "missed": sorted(invisible - leak_hit),
        },
        "injections": _prf(len(inj_hit), len(inj_wrong),
                           len(truth_inject - set(inj_flags))),
        "injections_detail": {
            "expected": len(truth_inject),
            "found": sorted(inj_hit),
            "missed": sorted(truth_inject - set(inj_flags)),
            "false_positive": sorted(inj_wrong),
        },
        # The sanitizer test. A flow through Validator.sanitizeId is NOT exploitable,
        # so anything here means guards.is_sanitizer did not do its job — the single
        # most informative precision failure available in this corpus.
        "sanitized_but_reported": sorted(inj_flags.keys() & sanitized),
        "findings_total": len(findings),
    }


def _table(golden: dict, actual: dict) -> str:
    rows = [
        ("resource leaks", golden["counts"]["expect_leak"],
         actual["leaks"]["tp"], actual["leaks"]["recall"], actual["leaks"]["precision"]),
        ("  of which graph-invisible", golden["counts"]["leaks_graph_cannot_see"],
         actual["graph_invisible_leaks"]["found"],
         actual["graph_invisible_leaks"]["recall"], None),
        ("sql injections", golden["counts"]["expect_injection"],
         actual["injections"]["tp"], actual["injections"]["recall"],
         actual["injections"]["precision"]),
        ("sanitized (must NOT report)", 0,
         len(actual["sanitized_but_reported"]), None, None),
    ]
    width = max(len(r[0]) for r in rows) + 2
    lines = [f"{'':<{width}}{'GOLDEN':>8}{'ACTUAL':>8}{'RECALL':>9}{'PREC':>8}",
             "-" * (width + 33)]
    for name, want, got, rec, prec in rows:
        lines.append(
            f"{name:<{width}}{want:>8}{got:>8}"
            f"{('-' if rec is None else f'{rec:.0%}'):>9}"
            f"{('-' if prec is None else f'{prec:.0%}'):>8}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default="test_corpora/java_interconnected/golden.json")
    ap.add_argument("--run", default="runs/analysis_corpus.json")
    args = ap.parse_args()

    gpath = args.golden if os.path.isabs(args.golden) else os.path.join(_ROOT, args.golden)
    rpath = args.run if os.path.isabs(args.run) else os.path.join(_ROOT, args.run)
    for path in (gpath, rpath):
        if not os.path.exists(path):
            print(f"missing: {path}")
            return 1

    golden = json.load(open(gpath, encoding="utf-8"))
    result = json.load(open(rpath, encoding="utf-8"))
    actual = score(golden, result)

    print("=" * 62)
    print("GOLDEN vs ACTUAL")
    print("=" * 62)
    print(_table(golden, actual))
    print(f"\nscope: {golden['scope']['leak_verdict_is_about']}")

    print("\n--- stage funnel " + "-" * 44)
    for stage in ("pass_a", "signals", "reach", "paths", "pass_b", "pass_c", "pass_d"):
        if stage in result:
            payload = result[stage]
            print(f"{stage:<10} {json.dumps(payload, default=str)[:170]}")

    print("\n--- detail " + "-" * 50)
    print(json.dumps(actual, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

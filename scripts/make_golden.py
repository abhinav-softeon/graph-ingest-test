"""Derive ground truth by READING the corpus, not by trusting the generator.

WHY NOT JUST USE manifest.json
The manifest records what the generator INTENDED to emit. Scoring against intent
measures the generator's self-consistency, not the analysis. This file re-derives
every expectation from the .java source on disk, so a golden number disagreeing
with the manifest is a real finding about the corpus rather than a rounding error.
It has already caught one: see SCOPE below.

SCOPE — THE ONE AMBIGUITY THAT HAD TO BE PINNED DOWN
Every DAO in this corpus opens a Statement/PreparedStatement and a ResultSet and
closes NEITHER, in all four shapes. So "does this method leak a JDBC resource?" is
true of all 30 and discriminates nothing. The variable the shapes actually vary is
the CONNECTION lifecycle, so that is what `expect_leak` means here.

This is not a technicality — it was measured. Asked about a CLEAN_FINALLY method
twice, the model answered "leak" once and "clean" once, with identical prose both
times: *"PreparedStatement and ResultSet are not explicitly closed but rely on
Connection.close() to close them."* Both readings are defensible. Per the JDBC spec
Connection.close() closes its Statements; with a POOLED connection, close() returns
it to the pool and frequently does not. Scoring without fixing the scope would have
recorded that ambiguity as model error.

So statement/ResultSet closure is recorded as `stmt_rs_unclosed` (true everywhere,
non-discriminating) and kept OUT of the leak verdict, and a finding that cites only
ps/rs on a connection-clean method is scored as `scope_disagreement`, not as a false
positive. Honest either way, and visible rather than silently absorbed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_body(text: str) -> str:
    """The single-arg load(String) body — the method the shapes vary.

    Braces are counted rather than regex-matched to the closing brace, because the
    body contains nested blocks (try/finally/if) and a lazy match stops at the first
    one, silently truncating exactly the control flow being judged.
    """
    start = text.find("public String load(String id) throws Exception {")
    if start == -1:
        return ""
    i = text.index("{", start)
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return ""


def classify(body: str) -> dict:
    """Shape and vulnerability posture, read off the actual control flow."""
    twr = bool(re.search(r"try\s*\(\s*Connection", body))
    closes_conn = "c.close()" in body
    has_finally = "finally" in body

    if twr:
        shape, leaks = "CLEAN_TWR", False
    elif closes_conn and has_finally:
        shape, leaks = "CLEAN_FINALLY", False
    elif closes_conn:
        # close() present but not guaranteed — the graph sees a release and is
        # satisfied; an exception between acquire and close skips it.
        shape, leaks = "LEAK_NO_FINALLY", True
    else:
        shape, leaks = "LEAK_NO_CLOSE", True

    # Concatenation into the query string. Parameterized (?) with setString is not
    # injection however dynamic the surrounding code looks.
    concatenates = bool(re.search(r'executeQuery\(\s*"[^"]*"\s*\+', body))
    sanitized = "Validator.sanitizeId" in body

    return {
        "shape": shape,
        "expect_leak": leaks,
        # Does a purely structural detector see SOME release? True whenever close()
        # appears at all — which is why LEAK_NO_FINALLY is the interesting class.
        "graph_sees_a_release": closes_conn or twr,
        "expect_injection": concatenates and not sanitized,
        "sanitized": sanitized,
        "concatenates_sql": concatenates,
        # Non-discriminating: true for every DAO here. Recorded so a finding that
        # cites it can be separated from a real disagreement about the connection.
        "stmt_rs_unclosed": not re.search(r"\b(ps|st|rs)\.close\(\)", body),
    }


def build(corpus: str) -> dict:
    dao_dir = os.path.join(corpus, "com", "testcorp", "dao")
    if not os.path.isdir(dao_dir):
        raise SystemExit(f"no DAO directory under {corpus}")

    rows = []
    for name in sorted(os.listdir(dao_dir),
                       key=lambda n: int(re.sub(r"\D", "", n) or 0)):
        if not name.endswith(".java"):
            continue
        text = open(os.path.join(dao_dir, name), encoding="utf-8").read()
        body = _load_body(text)
        if not body:
            print(f"[golden] WARNING: no load(String) body found in {name}")
            continue
        cls = name[:-5]
        rows.append({
            "fqn": f"com.testcorp.dao.{cls}#load",
            "file": f"dao/{name}",
            **classify(body),
        })

    leaks = [r for r in rows if r["expect_leak"]]
    invisible = [r for r in leaks if r["graph_sees_a_release"]]
    golden = {
        "derived_from": "source",
        "scope": {
            "leak_verdict_is_about": "the Connection lifecycle only",
            "why": (
                "Statement/PreparedStatement and ResultSet are unclosed in ALL "
                f"{sum(1 for r in rows if r['stmt_rs_unclosed'])} of {len(rows)} "
                "DAOs, so they cannot discriminate between shapes. A finding citing "
                "only ps/rs on a connection-clean method is a scope_disagreement, "
                "not a false positive."
            ),
        },
        "counts": {
            "daos": len(rows),
            "expect_leak": len(leaks),
            "leaks_graph_cannot_see": len(invisible),
            "expect_injection": sum(1 for r in rows if r["expect_injection"]),
            "sanitized_not_vulnerable": sum(1 for r in rows if r["sanitized"]),
            "by_shape": {s: sum(1 for r in rows if r["shape"] == s)
                         for s in sorted({r["shape"] for r in rows})},
        },
        "expected_leaks": sorted(r["fqn"] for r in leaks),
        "expected_leaks_graph_cannot_see": sorted(r["fqn"] for r in invisible),
        "expected_injections": sorted(r["fqn"] for r in rows if r["expect_injection"]),
        "must_not_report_injection": sorted(r["fqn"] for r in rows if r["sanitized"]),
        "daos": rows,
    }
    return golden


def compare_to_manifest(golden: dict, corpus: str) -> list[str]:
    """Disagreements between source-derived truth and the generator's manifest.

    Empty output means the generator does what it claims. Anything here is a corpus
    bug, and it matters more than a scoring delta: it means the labels every earlier
    measurement was scored against were wrong.
    """
    path = os.path.join(corpus, "manifest.json")
    if not os.path.exists(path):
        return ["no manifest.json to compare against"]
    manifest = json.load(open(path, encoding="utf-8"))
    claimed = {d["fqn"]: d for d in manifest.get("expected_dao_findings", [])}
    problems = []
    for row in golden["daos"]:
        want = claimed.get(row["fqn"])
        if not want:
            problems.append(f"{row['fqn']}: in source, absent from manifest")
            continue
        for field in ("shape", "expect_leak", "expect_injection", "sanitized"):
            if want.get(field) != row[field]:
                problems.append(
                    f"{row['fqn']}.{field}: manifest={want.get(field)!r} "
                    f"source={row[field]!r}")
    for fqn in claimed:
        if fqn not in {r["fqn"] for r in golden["daos"]}:
            problems.append(f"{fqn}: in manifest, absent from source")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="test_corpora/java_interconnected")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    corpus = args.corpus if os.path.isabs(args.corpus) else os.path.join(_ROOT, args.corpus)
    golden = build(corpus)
    out = args.out or os.path.join(corpus, "golden.json")

    problems = compare_to_manifest(golden, corpus)
    golden["manifest_disagreements"] = problems

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(golden, fh, indent=2)

    print(json.dumps(golden["counts"], indent=2))
    print(f"\nscope: {golden['scope']['leak_verdict_is_about']}")
    print(f"  {golden['scope']['why']}")
    if problems:
        print(f"\n!! {len(problems)} disagreement(s) with manifest.json:")
        for p in problems[:20]:
            print(f"   - {p}")
    else:
        print("\nmanifest.json agrees with the source on every DAO.")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

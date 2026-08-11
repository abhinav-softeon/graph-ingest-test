"""One real LLM call against one real file. No Neo4j, no graph, no pipeline.

WHY THIS EXISTS SEPARATELY FROM THE TEST SUITE
The unit tests prove the schema is well-formed and that validate() rejects what it
should. They cannot prove the thing that actually matters: that a model, handed
this prompt and constrained to this schema, produces observations a human agrees
with. That question needs a real call, and it is cheap — one file, one request.

Run it after any change to prompts.py or contract.py, on a file whose bugs you
already know. What you are checking, in order:

  1. Does the call come back at all, and does the response validate?
  2. Are `unguarded_calls` entries real, and are the obvious ones present?
  3. Do the finding kinds fire, or does everything land in 'other'?
  4. Are certainty/impact used across their range, or is everything
     'demonstrated'/'exposure'? A model that never says 'speculative' or
     'quality' is not calibrated, and the computed severity inherits that.

    python scripts/smoke_file_pass.py path/to/File.java
    python scripts/smoke_file_pass.py path/to/File.java --model us.amazon.nova-2-lite-v1:0
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# .env is not loaded by the engine itself (graph_core/config.py reads the service
# environment), so load it here or the credentials are simply absent.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from analysis import contract, priority, prompts  # noqa: E402
from analysis.llm import get_client  # noqa: E402

# Good enough to find method declarations for a smoke test. NOT a parser — the
# real pipeline gets these extents from tree-sitter. If it mis-detects here that
# costs one bad test run, not a bad graph.
_METHOD = re.compile(
    r"^[ \t]*(?:public|private|protected|static|final|synchronized|abstract|\s)*"
    r"[\w<>\[\],\s]+\s+(\w+)\s*\([^)]*\)\s*(?:throws [\w,\s.]+)?\{",
    re.M)


def find_functions(source: str, path: str) -> list[dict]:
    out = []
    for m in _METHOD.finditer(source):
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "synchronized", "new"):
            continue
        line = source[:m.start()].count("\n") + 1
        out.append({"id": f"{os.path.basename(path)}#{name}@{line}",
                    "name": name, "signature": m.group(0).strip().rstrip("{").strip(),
                    "start_line": line, "end_line": line})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--model", default=os.environ.get("GRAPH_SUMMARIZER_MODEL"))
    ap.add_argument("--show", type=int, default=3, help="summaries to dump in full")
    a = ap.parse_args()

    source = open(a.file, encoding="utf-8", errors="replace").read()
    funcs = find_functions(source, a.file)
    if not funcs:
        print("No methods detected — the smoke regex found nothing to ask about.")
        return
    print(f"file: {a.file}  ({len(source.splitlines())} lines, "
          f"{len(funcs)} function(s) detected)")
    print(f"model: {a.model}\n")

    system = prompts.system_prompt()
    user = prompts.build_user_prompt(a.file, "java", source, funcs)
    client = get_client(a.model, pass_name="smoke")
    res = client.complete(system, user, schema=contract.SUMMARY_SCHEMA)

    if not res.parsed:
        print("NO PARSEABLE RESPONSE")
        print("error:", res.error)
        print((res.text or "")[:1500])
        return

    try:
        rows = contract.validate(res.parsed, [f["id"] for f in funcs])
    except contract.ValidationError as exc:
        print(f"VALIDATION FAILED: {exc}\n")
        print(json.dumps(res.parsed, indent=2)[:2500])
        return

    print(f"validated {len(rows)} summar(ies)\n")

    # The four calibration questions from the docstring, answered as counts.
    kinds, certs, impacts, sevs = (collections.Counter() for _ in range(4))
    unguarded, nullable, leaks = [], [], []
    for r in rows:
        for f in r.get("findings") or []:
            kinds[f.get("kind")] += 1
            certs[f.get("certainty")] += 1
            impacts[f.get("impact")] += 1
            sevs[priority.severity(f.get("certainty"), f.get("impact"))] += 1
        c = r.get("contracts") or {}
        if c.get("may_return_null"):
            nullable.append((r["id"], c.get("null_condition") or ""))
        for name in c.get("unguarded_calls") or []:
            unguarded.append((r["id"], name))
        db = r.get("db") or {}
        if db.get("acquires") and not db.get("released_in_finally"):
            leaks.append(r["id"])

    def dump(title, counter):
        print(f"{title}: " + (", ".join(f"{k}={v}" for k, v in counter.most_common())
                              or "(none)"))

    print(f"findings: {sum(kinds.values())}")
    dump("  kinds     ", kinds)
    dump("  certainty ", certs)
    dump("  impact    ", impacts)
    dump("  severity  ", sevs)
    print(f"\nmay_return_null: {len(nullable)}")
    for fid, cond in nullable[:5]:
        print(f"    {fid}  ({cond})")
    print(f"unguarded_calls: {len(unguarded)}")
    for fid, name in unguarded[:8]:
        print(f"    {fid} -> {name}()")
    print(f"acquires without finally-release: {len(leaks)}")
    for fid in leaks[:5]:
        print(f"    {fid}")

    # The calibration warnings worth seeing immediately rather than in a dashboard.
    if kinds and kinds.get("other", 0) / max(sum(kinds.values()), 1) > 0.3:
        print("\n  WARNING: >30% of findings are kind='other' — the enum is missing "
              "members the model needs.")
    # Dominance, not uniqueness. The first version fired only when a counter had
    # exactly ONE distinct value, so a measured run of 14 'demonstrated' and 1
    # 'probable' passed silently -- the same uncalibrated axis with a rounding
    # error on it.
    for label, counter in (("certainty", certs), ("impact", impacts)):
        total = sum(counter.values())
        if total > 3:
            top, n = counter.most_common(1)[0]
            if n / total >= 0.8:
                print(f"\n  WARNING: {n}/{total} findings are {label}={top!r}. The axis "
                      f"is barely varying, so computed severity inherits that and "
                      f"collapses to one or two levels. Check several files before "
                      f"blaming the prompt -- one file can legitimately be uniform.")

    print(f"\n--- first {a.show} summar(ies) in full ---")
    for r in rows[:a.show]:
        print(json.dumps(r, indent=2))

    u = f"in={res.input_tokens} out={res.output_tokens} cache_r={res.cache_read_tokens} cache_w={res.cache_write_tokens} {res.seconds:.1f}s"
    if u:
        print(f"\nusage: {u}")


if __name__ == "__main__":
    main()

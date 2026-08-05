"""Run the full analysis on the generated corpus and SCORE IT against ground truth.

    python scripts/run_analysis.py                    # ingest + full pipeline
    python scripts/run_analysis.py --smoke-only       # just verify Bedrock works
    python scripts/run_analysis.py --skip-ingest      # reuse the existing graph
    python scripts/run_analysis.py --skip-pass-a      # reuse stored summaries

The point of running against the generated corpus rather than a real repo is that
manifest.json says which functions are genuinely vulnerable. So this prints
precision and recall, not a list of findings to eyeball — and specifically it
reports whether the LLM layer closed the recall gap the graph alone cannot
(release present but skipped on the exception path).

The Bedrock smoke test runs FIRST and costs a handful of tokens. It catches the
things that otherwise fail 20 minutes into a run: missing credentials, a region
without model access, and the per-model thinking shape (Haiku takes budget_tokens
and rejects effort; Sonnet 5 is the reverse — the wrong one is a 400).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The driver logs a multi-line WARNING for every query mentioning a property or
# relationship type that does not exist YET — `sig_schema_version` before the first
# Pass A, `WRITES` on a repo with no field writes. Both are expected, both fire once
# per query, and together they bury the stage logs completely: the first run of this
# script produced ~12KB of notification text before Pass A had summarized one file.
# Silenced here rather than in the library, because in an interactive session those
# notifications are genuinely useful and it is only this batch run that drowns in them.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def load_env(path: str = ".env") -> int:
    """Load .env into os.environ without overwriting anything already set.

    Existing env wins so a shell override still works; a real secrets manager in
    production would take the same precedence."""
    full = os.path.join(_ROOT, path)
    if not os.path.exists(full):
        print(f"[env] no {path} found — relying on the ambient environment")
        return 0
    loaded = 0
    with open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    print(f"[env] loaded {loaded} setting(s) from {path}")
    return loaded


def check_credentials() -> bool:
    """Fail fast and specifically. A blank key produces a confusing SDK error much
    later, usually after the ingest has already run."""
    missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
               if not os.environ.get(k, "").strip()]
    if missing:
        print(f"\n[FAIL] {', '.join(missing)} not set.")
        print("       Fill them in .env (it is gitignored) and re-run.")
        print("       Region must also be one where Bedrock model access is granted "
              "for these models — that is per-region in the Bedrock console.")
        return False
    print(f"[creds] AWS_ACCESS_KEY_ID set, region={os.environ.get('AWS_REGION')}")
    return True


def smoke_test() -> bool:
    """One tiny call per model, verifying the thinking config the code will use.

    Deliberately exercises `complete()` rather than the raw SDK, so what is proven
    working is the same path the passes take."""
    from analysis import config
    from analysis.llm import get_client

    ok = True
    for label, model in (("summarizer", config.summarizer_model()),
                         ("adjudicator", config.adjudicator_model())):
        thinking = config.thinking_config(model)
        effort = config.effort_config(model)
        print(f"\n[smoke] {label}: {model}")
        print(f"        thinking={thinking}  effort={effort}")
        try:
            client = get_client(model)
        except Exception as exc:  # noqa: BLE001
            print(f"        [FAIL] client construction: {exc}")
            ok = False
            continue
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["answer"],
                  "properties": {"answer": {"type": "string"}}}
        res = client.complete(
            "You answer with valid JSON only.",
            "Return {\"answer\":\"ok\"} and nothing else.",
            schema=schema,
        )
        if not res.ok:
            print(f"        [FAIL] {res.error}")
            if "AccessDenied" in (res.error or ""):
                print("        -> model access is not granted for this model in "
                      f"{os.environ.get('AWS_REGION')}. Enable it in the Bedrock "
                      "console, or switch region.")
            ok = False
            continue
        print(f"        [OK] {res.output_tokens} out tok, {res.seconds:.1f}s, "
              f"parsed={res.parsed}")
        print(f"        cache write={res.cache_write_tokens} read={res.cache_read_tokens}"
              + ("  (0 is expected on Haiku: 4096-token minimum prefix)"
                 if not res.cache_write_tokens else ""))
    return ok


def ingest(corpus: str, repo: str) -> dict:
    """Build the graph. Autocompiles with `javac -g` when no classes exist."""
    from graph_core.config import neo4j_config
    from graph_core.pipeline import index_repo
    from graph_core.store import GraphStore

    store = GraphStore(neo4j_config())
    store.bootstrap()
    print(f"\n[ingest] {corpus} -> repo={repo}")
    t0 = time.monotonic()
    result = index_repo(corpus, repo, store, wipe=True, javac=False, bytecode=True)
    print(f"[ingest] {result.files} file(s), {result.nodes} node(s), "
          f"{result.edges} edge(s) in {time.monotonic() - t0:.1f}s")
    bc = result.bytecode
    if bc is not None:
        print(f"[ingest] bytecode available={bc.available} "
              f"match_rate={bc.match_rate:.1%} coverage={bc.file_coverage:.1%} "
              f"synthesized={bc.synthesized_nodes} "
              f"(skipped_no_lines={bc.synthesis_skipped_no_lines})")
        if not bc.available:
            print(f"[ingest] WARNING bytecode unavailable: {bc.reason}")
            print("         Everything downstream filters to strategy='bytecode', "
                  "so the analysis will find almost nothing.")
    return {"store": store, "result": result}


def score(store, repo: str, corpus: str, report: dict) -> dict:
    """Compare reported findings against manifest.json.

    The number that matters is recall on `leaks_graph_cannot_see` — the leaks where
    a release IS present so the graph is satisfied. The graph alone scores 0 there
    by construction; anything above 0 is the summary layer earning its cost.
    """
    manifest_path = os.path.join(corpus, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"scored": False, "reason": "no manifest.json"}
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    expected = {d["fqn"]: d for d in manifest["expected_dao_findings"]}
    truth_leaks = {f for f, d in expected.items() if d["expect_leak"]}
    truth_inject = {f for f, d in expected.items() if d["expect_injection"]}
    sanitized = {f for f, d in expected.items() if d["sanitized"]}
    invisible = {f for f, d in expected.items()
                 if d["expect_leak"] and d["graph_sees_a_release"]}

    # A finding is credited to a function if that function appears anywhere on the
    # reported path — the vulnerable frame is what matters, not which entry point
    # happened to be the shortest route to it.
    def hit(fqns: set[str], kinds: set[str]) -> set[str]:
        out = set()
        for f in report.get("findings", []):
            if f.get("kind") not in kinds:
                continue
            for name in (f.get("path_fqns") or []) + [f.get("sink") or ""]:
                if name in fqns:
                    out.add(name)
        return out

    leak_hits = hit(truth_leaks | (set(expected) - truth_leaks), {"resource_leak"})
    inj_hits = hit(set(expected), {"sql_injection"})

    def prf(hits: set[str], truth: set[str], universe: set[str]) -> dict:
        tp = len(hits & truth)
        fp = len(hits & (universe - truth))
        fn = len(truth - hits)
        return {"tp": tp, "fp": fp, "fn": fn,
                "recall": round(tp / len(truth), 3) if truth else None,
                "precision": round(tp / (tp + fp), 3) if (tp + fp) else None}

    out = {
        "scored": True,
        "leaks": prf(leak_hits, truth_leaks, set(expected)),
        "injections": prf(inj_hits, truth_inject, set(expected)),
        "graph_invisible_leaks": {
            "total": len(invisible),
            "found": len(leak_hits & invisible),
            "recall": round(len(leak_hits & invisible) / len(invisible), 3)
            if invisible else None,
        },
        "sanitized_false_positives": sorted(inj_hits & sanitized),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="test_corpora/java_interconnected")
    ap.add_argument("--repo", default="corpus")
    ap.add_argument("--smoke-only", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-pass-a", action="store_true")
    ap.add_argument("--skip-smoke", action="store_true")
    args = ap.parse_args()

    load_env()
    if not check_credentials():
        return 2
    if not args.skip_smoke and not smoke_test():
        print("\n[abort] smoke test failed — fix the above before running the "
              "pipeline, which would fail the same way after doing the ingest.")
        return 3
    if args.smoke_only:
        print("\n[done] smoke test only.")
        return 0

    corpus = os.path.join(_ROOT, args.corpus) if not os.path.isabs(args.corpus) else args.corpus
    if not os.path.isdir(corpus):
        print(f"[FAIL] corpus not found: {corpus}")
        print("       Generate it: python scripts/gen_test_corpus.py")
        return 2

    from analysis import pipeline
    from graph_core.config import neo4j_config
    from graph_core.store import GraphStore

    if args.skip_ingest:
        store = GraphStore(neo4j_config())
        print("[ingest] skipped — reusing the existing graph")
    else:
        store = ingest(corpus, args.repo)["store"]

    print("\n[analysis] starting pipeline")
    result = pipeline.run(store, args.repo, corpus, skip_pass_a=args.skip_pass_a,
                          persist_dismissals=False)

    print("\n" + "=" * 74)
    print("STAGE REPORTS")
    print("=" * 74)
    for stage in ("pass_a", "signals", "reach", "pass_b", "pass_c", "pass_d"):
        if stage in result:
            print(f"\n{stage}:\n{json.dumps(result[stage], indent=2, default=str)}")

    report = result.get("report", {})
    print("\n" + "=" * 74)
    print("SCORED AGAINST GROUND TRUTH")
    print("=" * 74)
    print(json.dumps(score(store, args.repo, corpus, report), indent=2))
    print(f"\ncounts: {json.dumps(report.get('counts', {}), indent=2)}")

    out_path = os.path.join(_ROOT, "runs", f"analysis_{args.repo}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\nfull result -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

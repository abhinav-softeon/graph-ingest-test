"""End-to-end analysis run: A -> reach -> paths -> B -> C -> D -> report.

The order is load-bearing, not stylistic:

  A. Summarize every function, one file per call, incremental by body_hash.
  R. Mark reachability to a fixpoint. No depth bound; this is a closure.
  P. Enumerate paths INSIDE the universe R produced, bounded, hubs excluded.
  B. Judge paths from summaries alone. Cheap, and where most paths resolve.
  C. Fetch real source only where B said it could not tell.
  D. Adversarial panel tries to refute what survived.
  F. Dedupe, apply prior dismissals, rank.

Each stage narrows the set, so the expensive passes only ever see what the cheap
ones could not settle.
"""
from __future__ import annotations

import time

from . import (findings, neighborhood, pass_a, pass_b, pass_c, pass_d, paths,
               priority, reach, single_file)
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


def _sinks(findings: list[dict]) -> dict[str, dict]:
    """Index findings by the function they blame, keeping the richest one."""
    out: dict[str, dict] = {}
    for f in findings or []:
        key = f.get("sink") or ""
        if key and (key not in out or f.get("evidence")):
            out[key] = f
    return out


def _stage_trace(rows: list[dict], b, c, d) -> dict:
    """Per-function record of how far it got, so a miss can be attributed.

    Reports the four places a finding can disappear, in pipeline order:
      enumerated      — appeared on a path at all (absent = reach/paths dropped it)
      pass_b_finding  — Pass B called it a defect or asked for source
      pass_c_dismissed— refuted after reading real source (the strongest dismissal)
      pass_d_killed   — refuted by the adversarial panel, with the votes that did it
    """
    enumerated = sorted({f for r in rows for f in (r.get("fqns") or [])})
    b_sinks = _sinks(getattr(b, "findings", []))
    c_dismissed = {k: v for k, v in _sinks(getattr(c, "findings", [])).items()
                   if v.get("dismissed")}
    killed = _sinks(getattr(d, "killed_findings", []))
    confirmed = _sinks(getattr(d, "confirmed_findings", []))
    return {
        "enumerated_functions": len(enumerated),
        "pass_b_sinks": sorted(b_sinks),
        "pass_c_dismissed": sorted(c_dismissed),
        "pass_d_killed": {
            k: {"kind": v.get("kind"),
                "votes": (v.get("adjudication") or {}).get("votes"),
                "why": (v.get("dismissed_because") or "")[:300]}
            for k, v in killed.items()
        },
        "pass_d_confirmed": sorted(confirmed),
    }


def run(store, repo: str, root: str,
        langs: list[str] | None = None,
        sink_kinds: list[str] | None = None,
        max_depth: int = paths.DEFAULT_MAX_DEPTH,
        path_limit: int = 2000,
        include_leaks: bool = True,
        skip_pass_a: bool = False,
        persist_dismissals: bool = True) -> dict:
    """The whole pipeline. Safe to re-run — A is incremental, dismissals persist.

    ``skip_pass_a`` reuses existing summaries, for iterating on B/C/D prompts
    without re-paying for summarization.
    """
    t0 = time.monotonic()
    out: dict = {"repo": repo}

    if not skip_pass_a:
        out["pass_a"] = pass_a.run_pass_a(store, repo, root, langs=langs).summary()
    else:
        _log.info("[pipeline] skipping Pass A — reusing stored summaries")
        # Summaries written under an older schema read as fresh (body_hash still
        # matches) while missing fields the passes below rely on, which surfaces as
        # zero findings rather than as an error. Counted before anything consumes them.
        out["stale_schema"] = priority.stale_schema(store, repo)

    # Calibration, not decoration: if `important` fires on most of the repo it is
    # selecting nothing, and every ranking downstream is arbitrary. Recorded in the
    # report so a run can be judged after the fact, not just from live logs.
    out["signals"] = priority.signal_rates(store, repo)

    out["reach"] = reach.mark_all(store, repo, sink_kinds)
    universe_fraction = out["reach"]["universe"].get("universe_fraction", 0)
    if universe_fraction > 0.9:
        _log.warning(
            "[pipeline] universe is %.0f%% of the repo — the sink seeds are almost "
            "certainly too broad. Most likely GRAPH_EXTERNAL_ALL_CALLS is on, so "
            "benign library calls count as sinks and the pruning does nothing.",
            universe_fraction * 100,
        )

    # Harvested BEFORE the judging passes, not after, so each of them can be told
    # what is already known and asked only for the delta. Previously this ran last
    # and every pass rediscovered the same single-body defects independently.
    direct = single_file.harvest(store, repo)
    out["single_file"] = {"harvested": len(direct)}

    hubs = paths.find_hubs(store, repo)
    hub_ids = [h["id"] for h in hubs]
    out["hubs_excluded"] = [{"fqn": h["fqn"], "callers": h["callers"]} for h in hubs[:20]]

    sink_rows = paths.sink_paths(store, repo, sink_kinds, max_depth, path_limit, hub_ids)
    leak_rows = paths.leak_paths(store, repo, max_depth, path_limit // 2, hub_ids)         if include_leaks else []
    raw = sink_rows + leak_rows
    rows = paths.dedupe_paths(raw)
    # The path funnel, reported rather than inferred. "How many paths existed and how
    # many were actually judged" is the question that says whether a run covered the
    # repo or silently truncated: enumeration is bounded by max_depth and a LIMIT,
    # dedup collapses sub-paths of the same chain, and only what survives both is ever
    # sent to a model. A single "paths: 205" hides all three.
    depths: dict[str, int] = {}
    for r in rows:
        depths[str(r.get("hops"))] = depths.get(str(r.get("hops")), 0) + 1
    out["path_stats"] = {
        "entry_to_sink_enumerated": len(sink_rows),
        "leak_paths_enumerated": len(leak_rows),
        "raw_total": len(raw),
        "after_dedupe": len(rows),
        "dropped_as_subpaths": len(raw) - len(rows),
        "max_depth_bound": max_depth,
        "deepest_seen": max([r.get("hops") or 0 for r in rows], default=0),
        "by_depth": dict(sorted(depths.items(), key=lambda kv: int(kv[0]))),
        "hit_limit": len(sink_rows) >= path_limit,
    }
    out["paths"] = len(rows)

    if not rows:
        _log.warning("[pipeline] no paths to judge — stopping before Pass B")
        out["seconds"] = round(time.monotonic() - t0, 1)
        return out

    b = pass_b.run_pass_b(store, repo, rows, prior_findings=direct, root=root)
    out["pass_b"] = b.summary()

    c = pass_c.run_pass_c(store, repo, root, b.findings)
    out["pass_c"] = c.summary()

    # Neighbourhood pass: every summarized function judged against its 1- and 2-hop
    # callee summaries, independent of whether it sits on an entry->sink path. Pass B
    # only covers the reachability universe (43% of functions on this corpus), so
    # without this the majority of the repo is seen exactly once, by Pass A, with no
    # view of what it calls.
    nb = neighborhood.run_neighborhood(store, repo, prior_findings=direct)
    out["neighborhood"] = nb.summary()

    # Neighbourhood findings that asked for source join Pass C's queue, then both
    # streams are adjudicated together so one dedup and one panel cover everything.
    nb_expand = [f for f in nb.findings if f.get("need_source_for")]
    if nb_expand:
        nc = pass_c.run_pass_c(store, repo, root, nb_expand)
        out["pass_c_neighborhood"] = nc.summary()
        nb_ready = [f for f in nb.findings if not f.get("need_source_for")] + nc.findings
    else:
        nb_ready = nb.findings

    # DEDUPE BEFORE ADJUDICATION, not after. Measured: 496 findings entered Pass D,
    # only 171 were distinct defects, and the panel spent 975 calls (~$5.64) refuting
    # the same bug reached by a different path. Dedup was running after D purely
    # because that is where the report is assembled.
    candidates = findings.dedupe(store, repo, c.findings + nb_ready)
    out["pre_adjudication"] = {
        "before_dedupe": len(c.findings) + len(nb_ready),
        "distinct": len(candidates),
    }
    d = pass_d.run_pass_d(candidates)
    out["pass_d"] = d.summary()

    # STAGE TRACE — which stage dropped each vulnerable function, kept per sink.
    # Without it, "a finding is missing" gives no purchase: every stage narrows the
    # set, so a miss looks identical whether the path was never enumerated, judged
    # clean from summaries, refuted with source, or killed by the panel. Guessing
    # from the aggregate counts produced one wrong diagnosis already. Cheap, no
    # model calls, and it makes the next question answerable from the artifact.
    out["trace"] = _stage_trace(rows, b, c, d)

    # Single-body defects bypass adjudication entirely: they were decided by the only
    # pass that reads the whole file, and routing them through the panel cost 7 of 15
    # leaks when measured. They join here for one shared dedup, ranking and dismissal.
    out["report"] = findings.report(store, repo, d.confirmed_findings + direct,
                                   d.killed_findings, persist=persist_dismissals)
    out["seconds"] = round(time.monotonic() - t0, 1)
    _log.info("[pipeline] complete in %.1fs — %s finding(s) reported",
              out["seconds"], out["report"]["counts"]["reported"])
    return out

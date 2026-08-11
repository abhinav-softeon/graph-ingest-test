"""Pass B — judge assembled paths by joining summaries. No source code sent.

THE ECONOMY OF THIS PASS
Pass A already read every function once. Pass B never re-reads source: it sends the
STRUCTURED SUMMARIES of the functions on a path, in order, and asks whether
untrusted data actually survives the chain. A path of 6 functions costs ~6 x 300
tokens of summary instead of ~6 x 2000 tokens of source, which is what makes
judging thousands of paths affordable.

WHAT MAKES THE JOIN WORK
`params[].flows_to` from Pass A. Without the param -> callee-argument mapping the
summaries are six unrelated descriptions; with it they compose into a flow. That
one field is the reason the summary schema looks the way it does.

WHERE THIS PASS DELIBERATELY STOPS
When a summary is not enough, the model is asked to name what it needs in
`need_source_for` rather than guess. Those names are Pass C's work queue. A model
that says "I cannot tell without seeing sanitize()" is producing a useful result;
one that guesses is producing a false positive with a confident tone.

CACHING ACTUALLY HELPS HERE, UNLIKE PASS A
Hub summaries recur across many paths, and batches are grouped by shared sink
(paths.batch_paths), so the same summaries repeat within and across calls. The
system prompt plus the sink description form a stable prefix — worth checking
cache_read_tokens on a real run, since unlike Pass A there is genuine reuse.
"""
from __future__ import annotations

import os

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, contract, store as astore
from . import priority
from .llm import get_client
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


def read_body(root: str, relpath: str, start: int, end: int) -> str | None:
    """Exact source span for a function, line-numbered.

    Moved here from the old source-expansion stage when that stage was deleted:
    this pass reads real source for every path rather than only for the ones a
    summary-based judge got stuck on, so the fetch belongs to the pass that does
    it. Positions come from tree-sitter and are exact, so this slices precisely
    rather than guessing at boundaries.
    """
    path = os.path.join(root, (relpath or "").replace("/", os.sep))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        _log.warning("[path_pass] unreadable %s: %s", relpath, exc)
        return None
    s = max(1, int(start or 1))
    e = min(len(lines), int(end or s))
    return "".join(f"{i:>5} | {lines[i - 1]}" for i in range(s, e + 1))


SYSTEM = """\
You judge whether a call path in a codebase contains a real defect.

You are given call paths. For each path you get the ordered functions from the
entry point to the dangerous operation, and for each function a STRUCTURED SUMMARY
produced by reading its source: what it does, where each parameter flows, whether
it validates or escapes, and what it touches.

TWO SEPARATE VERDICTS, AND THIS DISTINCTION MATTERS MOST
  `exploitable` — can UNTRUSTED DATA travel this chain and reach the dangerous
                  operation? This is a taint question and nothing else.
  `is_defect`   — is there a REAL BUG here at all, whether or not an attacker can
                  trigger it?

They are independent. A connection released only on the happy path is
`is_defect: true, exploitable: false` — nothing attacker-controlled is involved,
and it is still a bug that will exhaust the pool in production. Answering only the
taint question would discard it.

Every exploitable path is also a defect, so `exploitable: true` implies
`is_defect: true`. Set `is_defect: false` only when the path is genuinely fine.

HOW TO REASON
- Follow the parameter mapping. `flows_to: ["arg2 of dao.query"]` means this
  function's parameter becomes the third argument of the next frame. Trace it.
- A sanitizer anywhere on the path breaks the flow. If a frame validates, escapes,
  or parameterizes the value, the path is NOT exploitable — name that frame in
  `sanitized_at`.
- Parameterized SQL is not injection. `sql_is_dynamic: false` means placeholders
  are used and the value cannot alter the query's structure.
- A value that is never attacker-controlled is not a vulnerability, even if it
  reaches a sink. Constants, internal ids and server-side state are not untrusted.
- For a resource leak, the question is whether the release happens on EVERY path.
  `released_in_finally: false` with `acquires: true` means an exception between
  acquire and close leaks the resource. Report it as
  `is_defect: true, kind: "resource_leak", exploitable: false` — the leak is real
  even though no attacker data is involved and a close() exists.

WHEN YOU CANNOT TELL
Say so. Put the function names whose actual source you need into
`need_source_for` and set BOTH `exploitable` and `is_defect` to false. Their source
will be fetched and you will be asked again with the real code, so an unsure answer
here costs nothing and a guess costs correctness. Do NOT guess about a body you
have not seen, and do not infer behavior from a function's name.

ALREADY-REPORTED FINDINGS
Some frames carry an ALREADY REPORTED line. Those defects were found by a pass that
read the full source of that function. Do not report them again — a repeat is not a
second finding, it is the same finding paying for another round of adjudication and
then being discarded. Report only what is new about THIS PATH: specifically, whether
untrusted data survives the chain, which is the one question no single-function pass
can answer.

Be strict about BOTH flags. A false positive costs a reviewer's trust; set
`exploitable: true` only when you can point to the specific frames carrying the
value, and `is_defect: true` only when you can name what is actually wrong."""


@dataclass
class PassBReport:
    paths_considered: int = 0
    paths_judged: int = 0
    paths_missing_summaries: int = 0
    calls_made: int = 0
    batches_rejected: int = 0
    exploitable: int = 0
    defects_not_exploitable: int = 0   # real bugs with no attacker involvement
    needs_expansion: int = 0     # verdicts asking for source -> Pass C queue
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "paths_considered": self.paths_considered,
            "paths_judged": self.paths_judged,
            "paths_missing_summaries": self.paths_missing_summaries,
            "calls_made": self.calls_made,
            "batches_rejected": self.batches_rejected,
            "exploitable": self.exploitable,
            "defects_not_exploitable": self.defects_not_exploitable,
            "needs_expansion": self.needs_expansion,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "errors": self.errors[:20],
            "seconds": round(self.seconds, 1),
        }


def source_mode() -> str:
    """How much real source Pass B carries: 'none' | 'sink' | 'all'.

    WHY 'sink' IS THE DEFAULT AND NOT 'all'
    Input is far cheaper than output — $0.33 vs $2.75 per 1M on Nova, $1 vs $5 on
    Haiku — so trading input for better verdicts is usually a good trade. But 'all'
    is not a small trade: a 7-frame path at ~2,000 tokens of source per frame is
    ~14,000 input tokens against ~2,550 today, roughly 16x, and the middle frames of
    a chain are plumbing whose summaries already say everything that matters.

    The sink is different. It is where the defect actually is, where `sql_is_dynamic`
    and the concatenation live, and where a summary is most likely to be the thing
    standing between a right and a wrong verdict. Sending that one body costs ~2,000
    tokens per path and removes most of the reason Pass C exists.

    GRAPH_PASS_B_SOURCE=all|none to change it.
    """
    import os
    mode = os.environ.get("GRAPH_PASS_B_SOURCE", "sink").strip().lower()
    return mode if mode in ("none", "sink", "all") else "sink"


def _render_path(index: int, path: dict, summaries: dict[str, dict],
                 known: dict[str, list[str]] | None = None,
                 bodies: dict[str, str] | None = None) -> str | None:
    """One path as prompt text, or None if any frame lacks a fresh summary.

    Returning None rather than a partial rendering is deliberate: a chain with a
    hole in the middle invites the model to bridge the gap by assumption, which is
    exactly the failure this pass is built to avoid. A skipped path is counted and
    fixed by running Pass A, not papered over.
    """
    ids = path.get("ids") or []
    if not ids or any(i not in summaries for i in ids):
        return None
    lines = [f"### PATH {index}  ({len(ids)} frame(s))"]
    sink_kinds = path.get("sink_kinds") or []
    if sink_kinds:
        lines.append(f"Dangerous operation reached: {', '.join(sink_kinds)}"
                     f" via {', '.join(path.get('sink_names') or [])}")
    for depth, fid in enumerate(ids):
        s = summaries[fid]
        db = s.get("db") or {}
        params = "; ".join(
            f"{p.get('name')}"
            f"{' [validated]' if p.get('validated') else ''}"
            f" -> {', '.join(p.get('flows_to') or []) or 'nowhere'}"
            for p in (s.get("params") or [])
        ) or "(no parameters)"
        flags = []
        if db.get("acquires"):
            flags.append("acquires-db")
        if db.get("executes_sql"):
            flags.append("executes-sql" + ("(DYNAMIC)" if db.get("sql_is_dynamic") else "(parameterized)"))
        if db.get("acquires") and not db.get("released_in_finally"):
            flags.append("release-NOT-guaranteed")
        if db.get("throws_between_acquire_and_release"):
            flags.append("can-throw-before-release")
        touches = [t for t in (s.get("touches") or []) if t != "none"]
        if touches:
            flags.append("touches:" + "/".join(touches))
        # Guards are rendered because they are how a path is correctly DISMISSED.
        # Without them the model sees untrusted data reaching SQL and has no way to
        # know a frame in between neutralized it, so it reports a vulnerability that
        # the code already prevents. A sanitizer on the path is the single most
        # important thing to show, which is why it is stated as an instruction and
        # not just a flag.
        guards = s.get("guards") or {}
        src = s.get("source") or {}
        if src.get("is_entry_point"):
            kinds = [k for k in (src.get("kinds") or []) if k != "none"]
            flags.append("ENTRY-POINT" + (f"({'/'.join(kinds)})" if kinds else ""))
        if src.get("reads_untrusted"):
            flags.append("reads-untrusted-input")
        if guards.get("is_sanitizer"):
            flags.append("SANITIZER — input is neutralized here; data downstream of "
                         "this frame is NOT attacker-controlled unless re-tainted")
        if guards.get("authenticates") or guards.get("authorizes"):
            flags.append("auth-check")
        if guards.get("sanitizers_called"):
            flags.append("calls-validators:" + "/".join(guards["sanitizers_called"][:4]))
        reasons = [r for r in ((s.get("risk") or {}).get("reasons") or []) if r != "none"]
        if reasons:
            flags.append("risk:" + "/".join(reasons))
        lines.append(
            f"  [{depth}] {path['fqns'][depth]}\n"
            f"      does: {s.get('does', '')}\n"
            f"      params: {params}\n"
            f"      returns: {s.get('returns', '')}\n"
            + (f"      flags: {', '.join(flags)}\n" if flags else "")
            # Findings an earlier pass already reported for THIS frame. Shown so the
            # model does not re-report them: a duplicate is not a second finding, it
            # is the same one paying for adjudication again and being dropped at the
            # end. Measured: 496 findings reached the panel, 171 were distinct.
            + "".join(f"      ALREADY REPORTED: {p}\n"
                      for p in ((known or {}).get(path['fqns'][depth]) or [])[:3])
            # Real source for the frames that decide the verdict. A summary is a
            # reading of the code; where the two disagree the code wins, and this is
            # the point at which having it removes a Pass C round trip entirely.
            + ((f"      --- ACTUAL SOURCE (authoritative — trust over the summary) ---\n"
                f"{(bodies or {}).get(fid, '')}\n")
               if (bodies or {}).get(fid) else "")
        )
    return "\n".join(lines)


def fetch_bodies(store, repo: str, root: str, rows: list[dict]) -> dict[str, str]:
    """Source for the frames Pass B should see, per source_mode().

    Read once per FUNCTION and shared across every path that touches it — a hub sink
    appears on many paths, and re-reading its body per path would multiply the input
    cost by the fan-in for no added information.
    """
    mode = source_mode()
    if mode == "none":
        return {}
    wanted: set[str] = set()
    for row in rows:
        ids = row.get("ids") or []
        if not ids:
            continue
        wanted.update(ids) if mode == "all" else wanted.add(ids[-1])
    if not wanted:
        return {}
    meta = store.read(
        """
        UNWIND $ids AS fid
        MATCH (f:CodeNode {id: fid})
        WHERE f.file IS NOT NULL
        RETURN f.id AS id, f.file AS file, f.start_line AS s, f.end_line AS e
        """,
        ids=sorted(wanted),
    )
    out: dict[str, str] = {}
    for m in meta:
        body = read_body(root, m["file"], m["s"], m["e"])
        if body:
            out[m["id"]] = body
    _log.info("[path_pass] source mode=%s — fetched %s of %s requested bodie(s)",
              mode, len(out), len(wanted))
    return out


def _judge_batch(client, batch: list[dict], summaries: dict[str, dict],
                 known: dict[str, list[str]] | None = None,
                 bodies: dict[str, str] | None = None
                 ) -> tuple[list[dict], list, list[int]]:
    """One LLM call for a batch of paths. Returns (verdicts, llm results, skipped)."""
    rendered, index_map, skipped = [], [], []
    for path in batch:
        idx = len(rendered)
        text = _render_path(idx, path, summaries, known, bodies)
        if text is None:
            skipped.append(batch.index(path))
            continue
        rendered.append(text)
        index_map.append(path)
    if not rendered:
        return [], [], skipped

    user = (f"Judge the following {len(rendered)} path(s). Return one verdict per "
            f"path, using the PATH index shown.\n\n" + "\n\n".join(rendered))
    results = []
    for attempt in (1, 2):
        res = client.complete(SYSTEM, user, schema=contract.PATH_VERDICT_SCHEMA)
        results.append(res)
        if not res.ok or res.parsed is None:
            _log.warning("[path_pass] batch call failed (attempt %s): %s",
                         attempt, res.error or "no JSON")
            continue
        try:
            verdicts = contract.validate_verdicts(res.parsed, len(rendered))
        except contract.ValidationError as exc:
            _log.warning("[path_pass] batch failed validation (attempt %s): %s", attempt, exc)
            continue
        # Re-attach the path each verdict refers to, so callers never have to
        # re-derive the mapping from an index.
        for v in verdicts:
            v["_path"] = index_map[v["path_index"]]
        return verdicts, results, skipped
    return [], results, skipped


def run_path_pass(store, repo: str, path_rows: list[dict], per_batch: int = 3,
               prior_findings: list[dict] | None = None,
               root: str | None = None,
               model: str | None = None) -> PassBReport:
    """Judge paths. Summaries must already exist (run Pass A first).

    ``path_rows`` comes from paths.sink_paths()/leak_paths(), ideally through
    paths.dedupe_paths() and paths.batch_paths().
    """
    from . import paths as pmod

    model = model or config.adjudicator_model()
    rep = PassBReport()
    rep.paths_considered = len(path_rows)
    t0 = time.monotonic()

    if not path_rows:
        rep.seconds = time.monotonic() - t0
        _log.info("[path_pass] no paths supplied — nothing to judge")
        return rep

    all_ids = sorted({i for row in path_rows for i in (row.get("ids") or [])})
    summaries = astore.load_summaries(store, all_ids)
    missing = len(all_ids) - len(summaries)
    if missing:
        _log.warning(
            "[path_pass] %s of %s functions on these paths have no fresh summary — "
            "those paths are skipped, not guessed. Run Pass A to close the gap.",
            missing, len(all_ids),
        )

    # What earlier passes already reported, per function, so this pass returns
    # only the taint question it alone can answer.
    known: dict[str, list[str]] = {}
    for f in prior_findings or []:
        key = f.get('sink') or f.get('entry') or ''
        if key:
            known.setdefault(key, []).append(
                f"{f.get('kind')}: {(f.get('reasoning') or '')[:120]}")

    bodies = fetch_bodies(store, repo, root, path_rows) if root else {}

    batches = pmod.batch_paths(path_rows, per_batch)
    if not batches:
        rep.seconds = time.monotonic() - t0
        return rep

    client = get_client(model, pass_name="path_pass")
    _log.info("[path_pass] %s path(s) in %s batch(es); model=%s",
              len(path_rows), len(batches), model)

    with ThreadPoolExecutor(max_workers=config.llm_workers()) as pool:
        futures = [pool.submit(_judge_batch, client, b, summaries, known, bodies)
                       for b in batches]
        for fut in as_completed(futures):
            try:
                verdicts, results, skipped = fut.result()
            except Exception as exc:  # noqa: BLE001
                _log.warning("[path_pass] batch raised: %s", exc)
                rep.errors.append(str(exc))
                continue
            rep.calls_made += len(results)
            rep.paths_missing_summaries += len(skipped)
            for res in results:
                rep.input_tokens += res.input_tokens
                rep.output_tokens += res.output_tokens
                rep.cache_read_tokens += res.cache_read_tokens
            if not verdicts:
                rep.batches_rejected += 1
                continue
            for v in verdicts:
                rep.paths_judged += 1
                unresolved = bool(v.get("need_source_for"))
                if unresolved:
                    rep.needs_expansion += 1
                if v.get("exploitable"):
                    rep.exploitable += 1
                if v.get("is_defect") and not v.get("exploitable"):
                    rep.defects_not_exploitable += 1
                # THREE reasons to carry a verdict forward, and the last two were
                # missing. Gating on `exploitable` alone dropped every resource leak
                # (a defect, never a taint exploit) AND every "I need the source"
                # verdict — which is why Pass C received 0 findings while this pass
                # reported 35 needing expansion. An unresolved path is not innocent;
                # it is undecided, and deciding it is exactly Pass C's job.
                if v.get("exploitable") or v.get("is_defect") or unresolved:
                    rep.findings.append(_to_finding(v))

    rep.seconds = time.monotonic() - t0
    _log.info("[path_pass] done: %s", rep.summary())
    return rep


def _to_finding(verdict: dict) -> dict:
    """Flatten a verdict into a finding row for Pass D and the final report."""
    path = verdict.pop("_path", {}) or {}
    return {
        "kind": verdict.get("kind"),
        # Derived, not echoed: the verdict carries certainty + impact and the
        # table decides. Everything downstream still reads `severity`.
        "certainty": verdict.get("certainty"),
        "impact": verdict.get("impact"),
        "severity": priority.severity(verdict.get("certainty"), verdict.get("impact")),
        # Both flags travel with the finding: Pass C re-judges on them and Pass D
        # picks its lenses from them, so collapsing them here would just move the
        # same information loss one stage later.
        "exploitable": bool(verdict.get("exploitable")),
        "is_defect": bool(verdict.get("is_defect")),
        "entry": path.get("entry_fqn") or path.get("acquire_fqn"),
        # leak_paths() anchors on the ACQUIRING function and returns no
        # sink_fqn at all, so reading sink_fqn alone left every leak-path
        # finding with sink=None. findings.fingerprint then fell back to the
        # FILE, so dedup keyed on file rather than function — merging distinct
        # defects in one file — and scoring could only credit them by sweeping
        # the path, which silently inflates recall.
        "sink": path.get("sink_fqn") or path.get("acquire_fqn"),
        "sink_kinds": path.get("sink_kinds") or [],
        "file": path.get("sink_file") or path.get("acquire_file"),
        "line": path.get("sink_line") or path.get("acquire_line"),
        "path_ids": path.get("ids") or [],
        "path_fqns": path.get("fqns") or [],
        "hops": path.get("hops"),
        "reasoning": verdict.get("reasoning"),
        "evidence": verdict.get("evidence") or [],
        "sanitized_at": verdict.get("sanitized_at") or "",
        "need_source_for": verdict.get("need_source_for") or [],
    }

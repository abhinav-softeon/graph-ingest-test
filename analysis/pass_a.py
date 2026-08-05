"""Pass A — summarize every function, seeing each file once.

THE COST CONTRACT
One LLM call per file. The model sees the whole file (so it has class fields,
imports, constants and neighbouring methods as context) and returns one summary per
function in that file. A file is seen a SECOND time only for a reason:

  * it holds more functions than config.max_functions_per_call, so the request is
    chunked and the file text is re-sent per chunk; or
  * validation failed and the chunk is retried once.

Both are logged as `reread` so the "seen once" property is measurable rather than
asserted. A file whose functions all have fresh summaries is not read at all.

WHY VALIDATION IS NOT OPTIONAL
A summary that describes a function which does not exist, or silently omits one,
poisons everything above it — and neither failure is visible downstream. So every
response is checked against the exact id list before a single row is written, and a
chunk that fails twice is dropped with a warning rather than half-stored.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, contract, prompts, store as astore
from .llm import get_client
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


@dataclass
class PassAReport:
    files_seen: int = 0
    files_skipped_fresh: int = 0        # every function already had a valid summary
    functions_summarized: int = 0
    functions_skipped_fresh: int = 0
    calls_made: int = 0
    rereads: int = 0                   # calls beyond the first per file — chunking or retry
    chunks_rejected: int = 0           # failed validation twice, dropped
    throttled_calls: int = 0           # lost to quota, NOT to a bad response
    unknown_callees: int = 0
    claimed_callees: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "files_seen": self.files_seen,
            "files_skipped_fresh": self.files_skipped_fresh,
            "functions_summarized": self.functions_summarized,
            "functions_skipped_fresh": self.functions_skipped_fresh,
            "calls_made": self.calls_made,
            "rereads": self.rereads,
            "chunks_rejected": self.chunks_rejected,
            "throttled_calls": self.throttled_calls,
            "hallucinated_callee_rate": (
                round(self.unknown_callees / self.claimed_callees, 4)
                if self.claimed_callees else 0.0
            ),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "errors": self.errors[:20],
            "seconds": round(self.seconds, 1),
        }


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _read_source(root: str, relpath: str) -> str | None:
    path = os.path.join(root, relpath.replace("/", os.sep))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        _log.warning("[pass_a] unreadable %s: %s", relpath, exc)
        return None


def _run_chunk(client, model: str, relpath: str, lang: str, source: str,
               funcs: list[dict], facts: dict) -> tuple[list[dict], list, int]:
    """One LLM call. Returns (validated rows, llm results, attempts made).

    Retried once on a validation failure, because the common causes (a dropped
    function, a stray id) are non-deterministic and usually clear on a second
    sample. A second failure is dropped rather than partially trusted."""
    expected = [f["id"] for f in funcs]
    facts_text = "\n".join(
        line for f in funcs
        if (line := _facts_line(f, facts))
    )
    user = prompts.build_user_prompt(relpath, lang, source, funcs, facts_text)
    system = prompts.system_prompt()
    results = []

    for attempt in (1, 2):
        res = client.complete(system, user, schema=contract.SUMMARY_SCHEMA)
        results.append(res)
        if not res.ok:
            _log.warning("[pass_a] %s chunk call failed (attempt %s): %s",
                         relpath, attempt, res.error)
            continue
        if res.parsed is None:
            _log.warning("[pass_a] %s returned no parseable JSON (attempt %s)",
                         relpath, attempt)
            continue
        try:
            rows = contract.validate(res.parsed, expected)
        except contract.ValidationError as exc:
            _log.warning("[pass_a] %s failed validation (attempt %s): %s",
                         relpath, attempt, exc)
            continue
        return rows, results, attempt
    return [], results, 2


def _facts_line(func: dict, facts: dict) -> str:
    body = astore.render_facts(facts.get(func["id"], {}))
    if not body:
        return ""
    return f"{func.get('name') or func['id']}:\n{body}"


def run_pass_a(store, repo: str, root: str, langs: list[str] | None = None,
               model: str | None = None, limit_files: int | None = None) -> PassAReport:
    """Summarize the repo. Incremental by body_hash; safe to re-run.

    ``root`` is the extracted source tree — file bodies are read from disk rather
    than Neo4j, because source text is not persisted on the nodes (only its hash).
    """
    model = model or config.summarizer_model()
    rep = PassAReport()
    t0 = time.monotonic()

    files = astore.files_with_functions(store, repo, langs)
    if limit_files:
        files = files[:limit_files]
    _log.info("[pass_a] %s file(s) with functions; model=%s", len(files), model)

    jobs = []
    for entry in files:
        pending = astore.needs_summary(entry["functions"])
        fresh = len(entry["functions"]) - len(pending)
        rep.functions_skipped_fresh += fresh
        if not pending:
            rep.files_skipped_fresh += 1
            continue
        jobs.append((entry, pending))

    # A fully-cached re-run needs no model at all — every summary is still valid for
    # its body_hash. Returning here also means an incremental run works without the
    # provider SDK installed.
    if not jobs:
        rep.seconds = time.monotonic() - t0
        _log.info("[pass_a] every function already has a fresh summary "
                  "(%s function(s) across %s file(s)) — nothing to do",
                  rep.functions_skipped_fresh, rep.files_skipped_fresh)
        return rep

    client = get_client(model, pass_name="pass_a")
    per_call = config.max_functions_per_call()

    def _do_file(entry: dict, pending: list[dict]) -> dict:
        relpath, lang = entry["file"], entry["lang"]
        source = _read_source(root, relpath)
        if source is None:
            return {"error": f"unreadable: {relpath}"}
        facts = astore.graph_facts(store, repo, [f["id"] for f in pending])
        known = {fid: set(v.get("callees", [])) for fid, v in facts.items()}
        out_rows, results, calls, rereads, rejected = [], [], 0, 0, 0
        for idx, chunk in enumerate(_chunks(pending, per_call)):
            rows, res, attempts = _run_chunk(
                client, model, relpath, lang, source, chunk, facts)
            results.extend(res)
            calls += len(res)
            # Anything past the first call on this file is a second look at the
            # same source: a later chunk, or a retry.
            rereads += (len(res) - 1) if idx == 0 else len(res)
            if not rows:
                rejected += 1
                continue
            by_id = {f["id"]: f for f in chunk}
            for row in rows:
                out_rows.append({
                    "id": row["id"],
                    "summary": row,
                    "body_hash": by_id[row["id"]]["body_hash"],
                })
        unknown, total = contract.unknown_callee_rate(
            [r["summary"] for r in out_rows], known)
        return {"rows": out_rows, "results": results, "calls": calls,
                "rereads": rereads, "rejected": rejected,
                "unknown": unknown, "claimed": total, "file": relpath}

    workers = config.llm_workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_do_file, e, p): e["file"] for e, p in jobs}
        for fut in as_completed(futures):
            relpath = futures[fut]
            try:
                got = fut.result()
            except Exception as exc:  # noqa: BLE001 - one file must not kill the pass
                _log.warning("[pass_a] %s raised: %s", relpath, exc)
                rep.errors.append(f"{relpath}: {exc}")
                continue
            if got.get("error"):
                rep.errors.append(got["error"])
                continue
            rep.files_seen += 1
            rep.calls_made += got["calls"]
            rep.rereads += got["rereads"]
            rep.chunks_rejected += got["rejected"]
            rep.unknown_callees += got["unknown"]
            rep.claimed_callees += got["claimed"]
            for res in got["results"]:
                rep.throttled_calls += 1 if res.throttled else 0
                rep.input_tokens += res.input_tokens
                rep.output_tokens += res.output_tokens
                rep.cache_read_tokens += res.cache_read_tokens
                rep.cache_write_tokens += res.cache_write_tokens
            if got["rows"]:
                rep.functions_summarized += astore.write_summaries(
                    store, repo, got["rows"], model)

    rep.seconds = time.monotonic() - t0
    if rep.throttled_calls:
        # Loud, because the run still "succeeds" with fewer summaries. Every count
        # below this point — findings, recall, coverage — is then measured on an
        # incomplete repo, and nothing else in the output would reveal that.
        _log.warning(
            "[pass_a] %s call(s) LOST TO THROTTLING after retries. Those functions "
            "have no summary and every downstream number is on partial data. Lower "
            "GRAPH_LLM_WORKERS (currently %s) and re-run — cached summaries are not "
            "re-billed, so a re-run only fills the gaps.",
            rep.throttled_calls, config.llm_workers())
    if rep.calls_made and not rep.cache_write_tokens:
        # Expected on Haiku 4.5: the system prompt is ~1k tokens and the minimum
        # cacheable prefix is 4096. Stated once so it is not mistaken for a bug.
        _log.info(
            "[pass_a] no prompt caching engaged (cache_write=0) — expected when the "
            "system prompt is under the model's minimum cacheable prefix; the "
            "incremental saving here is body_hash, not caching",
        )
    _log.info("[pass_a] done: %s", rep.summary())
    return rep

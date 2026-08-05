"""The Pass A output contract: schema, and mechanical validation against the graph.

WHY THE SCHEMA LOOKS LIKE THIS
Every field is here because it answers a question the graph CANNOT answer, or
because a later pass has to join on it. Nothing is here for prose value:

  * params[].flows_to      joins summaries into a path. Without the
                           param -> callee-argument mapping you cannot thread
                           taint across a call boundary and two summaries sitting
                           next to each other tell you nothing.
  * db.released_in_finally THE field for unclosed-connection detection. No graph
                           edge models a finally block — CALLS_EXTERNAL says a
                           release happens *somewhere* in the function, not that
                           it happens on every path. Only source reading answers
                           it, which is the entire reason an LLM is in this loop.
  * db.sql_is_dynamic      injection candidates. Phase 4 P2 of the plan is
                           deliberately mark-don't-extract: the graph records
                           "builds queries dynamically", the model reads the code.
  * calls                  cross-checked against real CALLS edges. A claimed
                           callee the graph has never seen is a hallucination
                           caught for free, and it is the only cheap signal that
                           a summary is fabricated rather than merely wrong.
  * uncertain              the Pass C trigger. A model that says "cannot tell"
                           is far more useful than one that guesses, so there is
                           an explicit place to say it.

SCHEMA CONSTRAINTS ARE NOT STYLISTIC
Anthropic's structured outputs reject recursive schemas, numeric bounds
(minimum/maximum), and string bounds (minLength/maxLength), and REQUIRE
additionalProperties:false on every object. So the shape below is flat, bounded
by enums rather than lengths, and closed at every level. Adding a `maxLength` to
keep summaries short would make the whole request 400.
"""
from __future__ import annotations

# Sink kinds the model may report. Enum-constrained rather than free text so the
# reachability seeds downstream are a closed set that can be indexed and queried,
# not whatever phrasing the model chose that run.
TOUCHES = ["sql", "exec", "file", "deserialize", "response", "reflection", "none"]

# How untrusted data ENTERS. The mirror of TOUCHES, and the reason it exists:
# reach.mark_reaches_sink already seeds from the model's `touches`, but
# mark_from_entry seeds ONLY from structure (annotations, EXPOSES, JSP). A repo
# whose entry convention is not in ENTRY_ANNOTATIONS therefore yields an empty
# universe with nothing but a log line to say so. This closes that asymmetry.
SOURCE_KINDS = ["http_request", "rpc", "jsp", "message_queue", "file_input",
                "cli", "scheduled", "none"]

# Observable properties, NOT a severity judgment. Asking a model "is this function
# important?" gets yes for everything, because a per-file reader has no baseline to
# compare against. Asking "does it concatenate SQL?" gets an accurate answer. The
# importance SCORE is computed from these in priority.py, where it can be measured
# and tuned; only the observations come from the model.
RISK_REASONS = ["builds_sql_dynamically", "manual_resource_handling", "auth_check",
                "authz_check", "crypto", "deserialization", "parses_untrusted_input",
                "spawns_process", "writes_filesystem", "reflection",
                "transaction_boundary", "none"]

# Leak classes are per-resource: closing the Connection while leaking the
# ResultSet is a real and common bug, and a single `released_in_finally` boolean
# cannot express it.
RESOURCE_TYPES = ["Connection", "Statement", "ResultSet", "Session", "Stream", "none"]

_SUMMARY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "does", "params", "returns", "calls", "db", "touches",
                 "source", "risk", "guards",
                 "fields_read", "fields_written", "findings", "uncertain"],
    "properties": {
        "id": {
            "type": "string",
            "description": "The exact function id given in the request. Never invent one.",
        },
        "does": {
            "type": "string",
            "description": "What this function does, one sentence. No preamble.",
        },
        "params": {
            "type": "array",
            "description": "One entry per parameter, in declaration order.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "flows_to", "validated"],
                "properties": {
                    "name": {"type": "string"},
                    "flows_to": {
                        "type": "array",
                        "description": (
                            "Where this parameter's value ends up. Use exactly these "
                            "forms: 'return', 'field:<fieldName>', "
                            "'arg<N> of <callee>', 'sql', 'exec', 'file', "
                            "'response', 'discarded'. Empty if it goes nowhere."
                        ),
                        "items": {"type": "string"},
                    },
                    "validated": {
                        "type": "boolean",
                        "description": "Is this parameter checked, escaped or parameterized before use?",
                    },
                },
            },
        },
        "returns": {
            "type": "string",
            "description": "What the return value is, or 'void'. Note if it returns a Connection.",
        },
        "calls": {
            "type": "array",
            "description": (
                "Names of functions/methods called in this body, as written in the "
                "source. Cross-checked against the graph — do not guess."
            ),
            "items": {"type": "string"},
        },
        "db": {
            "type": "object",
            "additionalProperties": False,
            "required": ["acquires", "releases", "released_in_finally",
                         "executes_sql", "sql_is_dynamic", "resources_leaked",
                         "throws_between_acquire_and_release", "resource_types"],
            "properties": {
                "acquires": {
                    "type": "boolean",
                    "description": "Obtains a Connection/Session/EntityManager (directly or from a pool wrapper).",
                },
                "releases": {
                    "type": "boolean",
                    "description": "Calls close/commit/rollback on a DB resource anywhere in the body.",
                },
                "released_in_finally": {
                    "type": "boolean",
                    "description": (
                        "TRUE only if release happens on EVERY path — a finally block, "
                        "try-with-resources, or equivalent. FALSE if release is only on "
                        "the happy path, which is a leak on exception."
                    ),
                },
                "executes_sql": {"type": "boolean"},
                "sql_is_dynamic": {
                    "type": "boolean",
                    "description": (
                        "TRUE if any SQL string is built by concatenation or interpolation "
                        "of a non-constant. FALSE if fully parameterized or a constant."
                    ),
                },
                "resources_leaked": {
                    "type": "array",
                    "description": (
                        "DB resources opened here that are not closed on every path. "
                        "Name the variable, e.g. 'conn', 'stmt', 'rs'."
                    ),
                    "items": {"type": "string"},
                },
                "throws_between_acquire_and_release": {
                    "type": "boolean",
                    "description": (
                        "TRUE if any call between the acquire and the release can throw, "
                        "so an exception skips the release. This is the leak condition "
                        "stated directly rather than inferred."
                    ),
                },
                "resource_types": {
                    "type": "array",
                    "description": "Kinds of resource opened here. Empty if none.",
                    "items": {"type": "string", "enum": RESOURCE_TYPES},
                },
            },
        },
        "touches": {
            "type": "array",
            "description": "Dangerous operation kinds reached directly in this body.",
            "items": {"type": "string", "enum": TOUCHES},
        },
        "source": {
            "type": "object",
            "additionalProperties": False,
            "required": ["is_entry_point", "reads_untrusted", "kinds"],
            "properties": {
                "is_entry_point": {
                    "type": "boolean",
                    "description": (
                        "Is this function invoked from OUTSIDE the application — an HTTP "
                        "or web-service handler, a JSP page, a queue listener, a scheduled "
                        "job, a main method? Not merely public."
                    ),
                },
                "reads_untrusted": {
                    "type": "boolean",
                    "description": (
                        "Does this body read attacker-influenced data directly (request "
                        "parameters, headers, cookies, uploaded files, message bodies)? "
                        "A function can be an entry point without reading any, and can "
                        "read untrusted data without being an entry point."
                    ),
                },
                "kinds": {
                    "type": "array",
                    "description": "How data enters here. Empty or ['none'] if it does not.",
                    "items": {"type": "string", "enum": SOURCE_KINDS},
                },
            },
        },
        "risk": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reasons", "notes"],
            "properties": {
                "reasons": {
                    "type": "array",
                    "description": (
                        "Which of these are OBSERVABLY TRUE of this body. Report what you "
                        "see, not how dangerous you think the function is — the ranking is "
                        "computed elsewhere. ['none'] is a common and correct answer."
                    ),
                    "items": {"type": "string", "enum": RISK_REASONS},
                },
                "notes": {
                    "type": "string",
                    "description": "One sentence on the riskiest aspect, or empty string.",
                },
            },
        },
        "guards": {
            "type": "object",
            "additionalProperties": False,
            "required": ["is_sanitizer", "authenticates", "authorizes",
                         "validates_input", "sanitizers_called"],
            "properties": {
                "is_sanitizer": {
                    "type": "boolean",
                    "description": (
                        "Is this function's PURPOSE to validate, escape, encode or "
                        "otherwise make its input safe, returning a safe value or "
                        "rejecting? Taint stops here, so a wrong TRUE hides real bugs — "
                        "set it only when neutralizing input is what the function is for."
                    ),
                },
                "authenticates": {
                    "type": "boolean",
                    "description": "Verifies WHO the caller is (login, token/session check).",
                },
                "authorizes": {
                    "type": "boolean",
                    "description": "Verifies the caller is ALLOWED to do this (role/permission).",
                },
                "validates_input": {
                    "type": "boolean",
                    "description": "Checks its inputs before use, without being a sanitizer per se.",
                },
                "sanitizers_called": {
                    "type": "array",
                    "description": (
                        "Names of escaping/validating functions this body calls, e.g. "
                        "'escapeHtml', 'validateOrderId'. Empty if none."
                    ),
                    "items": {"type": "string"},
                },
            },
        },
        "fields_read": {"type": "array", "items": {"type": "string"}},
        "fields_written": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "description": "Defects visible in THIS body alone. Empty is a valid and common answer.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "line", "detail", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["sql_injection", "resource_leak", "command_injection",
                                 "path_traversal", "deserialization", "xss",
                                 "correctness", "concurrency", "error_handling", "other"],
                    },
                    "line": {"type": "integer", "description": "Absolute line number in the file."},
                    "detail": {"type": "string", "description": "What is wrong and why it matters."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "uncertain": {
            "type": "array",
            "description": (
                "What you could not determine from this file alone, and what you would "
                "need to see. Drives targeted expansion — prefer saying this over guessing."
            ),
            "items": {"type": "string"},
        },
    },
}

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summaries"],
    "properties": {"summaries": {"type": "array", "items": _SUMMARY}},
}

# Bump whenever _SUMMARY changes shape. body_hash detects stale CONTENT; it cannot
# detect a stale SHAPE — a summary produced under an older schema has a matching
# body_hash and is silently treated as fresh, so every consumer of a new field sees
# nothing and reports zero rather than an error. Stored per node as
# summary_schema_version, which lets the next schema change re-summarize only the
# nodes that lack the new fields instead of re-billing the whole repo.
#
# 1 -> 2: added source{}, risk{}, guards{}, db.throws_between_acquire_and_release,
#         db.resource_types.
SCHEMA_VERSION = 2


class ValidationError(Exception):
    """A response that cannot be trusted. Always a retry, never a store."""


def validate(payload: dict, expected_ids: list[str],
             known_callees: dict[str, set[str]] | None = None) -> list[dict]:
    """Check a Pass A response against the graph. Returns the summaries, or raises.

    This is what converts "the model might hallucinate" into a check that either
    passes or fails:

      1. Every returned id must be one we asked for. An invented id means the
         model is describing a function that does not exist.
      2. Every id we asked for must come back. A silently dropped function is a
         coverage hole, and coverage holes are invisible later.
      3. No duplicates — two summaries for one id means one silently wins.

    ``known_callees`` enables the cheap hallucination check: claimed callees that
    the graph has never seen are reported as a warning rather than a rejection,
    because the extractor's own recall is imperfect (a call the model correctly
    read may legitimately be missing from CALLS). Treat a high rate as a signal
    about one side or the other, not as proof about either.
    """
    if not isinstance(payload, dict):
        raise ValidationError(f"expected an object, got {type(payload).__name__}")
    rows = payload.get("summaries")
    if not isinstance(rows, list):
        raise ValidationError("missing 'summaries' array")

    expected = set(expected_ids)
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("summary entry is not an object")
        fid = row.get("id")
        if fid not in expected:
            raise ValidationError(
                f"unknown function id {fid!r} — not one of the {len(expected)} requested"
            )
        if fid in seen:
            raise ValidationError(f"duplicate summary for {fid!r}")
        seen.add(fid)

    missing = expected - seen
    if missing:
        raise ValidationError(
            f"{len(missing)} requested function(s) missing from the response: "
            f"{sorted(missing)[:5]}"
        )
    _check_blocks(rows)
    return rows


# Blocks whose absence is silent rather than loud: derive_signals() defaults every
# one of them to "inert", so a response that omits them produces summaries that
# validate, store, and read as "nothing interesting here" for the whole repo.
_SIGNAL_BLOCKS = ("source", "risk", "guards", "db")


def _check_blocks(rows: list[dict]) -> None:
    """Guard against a response that parses but carries none of the signal blocks.

    ONLY NEEDED BECAUSE ENFORCEMENT IS NOT ALWAYS AVAILABLE. On the Mantle endpoint
    output_config.format makes a missing block impossible. On the legacy
    InvokeModel path the schema is merely requested in the prompt, so the model can
    return well-formed JSON that omits it — and because the defaults are all
    falsy, the result is a repo where nothing is important, nothing is an entry
    point, and no path is ever selected. That reads exactly like a clean codebase.

    A block missing from EVERY summary is systematic — the model is not following
    the schema — so it raises and the call is retried. Sporadic omissions are left
    to the defaults, since one under-filled summary is not worth discarding a whole
    file's work over.
    """
    if not rows:
        return
    for block in _SIGNAL_BLOCKS:
        if all(not isinstance(r.get(block), dict) for r in rows):
            raise ValidationError(
                f"every summary omits the {block!r} block — the response is not "
                f"following the schema. Left unchecked this stores {len(rows)} "
                f"all-default summaries that read as 'nothing found'."
            )


# --- Pass B: path verdicts ---------------------------------------------------
# Keyed by path_index rather than by a path id string: the model only has to echo a
# small integer it was shown, which is far harder to get subtly wrong than a
# 16-char hash, and the mapping back is exact.
_PATH_VERDICT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path_index", "exploitable", "is_defect", "kind", "severity",
                 "sanitized_at", "reasoning", "evidence", "need_source_for"],
    "properties": {
        "path_index": {"type": "integer", "description": "The index shown in the request."},
        "exploitable": {
            "type": "boolean",
            "description": (
                "TRUE only if untrusted data can actually reach the dangerous "
                "operation along THIS chain. A sanitizer anywhere on the path, or a "
                "value that is never attacker-controlled, makes this FALSE. This is a "
                "TAINT question only — see is_defect for bugs that are not about "
                "attacker-controlled data."
            ),
        },
        # WHY THIS FIELD EXISTS, MEASURED
        # `exploitable` alone was the gate on becoming a finding, and it asks a taint
        # question. A resource leak is not a taint bug: nothing attacker-controlled
        # flows anywhere, so the correct answer is exploitable=false and every leak
        # was dropped. Pass A found 15 of 15 leaks; 1 survived to the report. One
        # boolean was carrying two incompatible meanings.
        "is_defect": {
            "type": "boolean",
            "description": (
                "TRUE if this path shows a REAL DEFECT of any kind, whether or not an "
                "attacker can trigger it. A connection released only on the happy path "
                "is a defect and is NOT exploitable — both flags are set independently. "
                "Every exploitable path is also a defect."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["sql_injection", "resource_leak", "command_injection",
                     "path_traversal", "deserialization", "xss", "none"],
        },
        "severity": {
            "type": "string",
            "enum": ["critical", "high", "medium", "low", "none"],
            "description": (
                "How bad this is, on a rubric with no overlap between levels. Pick by "
                "CERTAINTY first, then impact — the difference between critical and "
                "high is whether it definitely happens, and the difference between "
                "high and medium is whether you are speculating.\n"
                "critical - certain. You KNOW this breaks: the code cannot work, or a "
                "credential/API key/secret is exposed, or it is an unambiguous "
                "exploitable vulnerability. No conditions attached.\n"
                "high - serious but conditional. A security weakness, or a resource "
                "left unclosed, that WILL cause a failure when the wrong thing "
                "happens. Real, just not guaranteed on every run.\n"
                "medium - the same class of problem as high, but SPECULATIVE. You "
                "cannot show the conditions are reachable, or you are inferring "
                "rather than pointing at it.\n"
                "low - improvements and optimizations. Correct code that could be "
                "better: clarity, efficiency, duplication, defensive gaps.\n"
                "none - not a defect at all.\n"
                "Do NOT map severity onto kind. Every injection is not automatically "
                "critical and every leak is not automatically high — a leak on a path "
                "nothing reaches is medium at most, and an injection whose input is "
                "demonstrably attacker-controlled and unsanitized is critical."
            ),
        },
        "sanitized_at": {
            "type": "string",
            "description": "Function on the path that neutralizes the flow, or '' if none does.",
        },
        "reasoning": {"type": "string", "description": "Why, referring to specific frames."},
        "evidence": {
            "type": "array",
            "description": "Concrete anchors. A verdict with no evidence is an opinion.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["function", "line", "what"],
                "properties": {
                    "function": {"type": "string"},
                    "line": {"type": "integer"},
                    "what": {"type": "string"},
                },
            },
        },
        "need_source_for": {
            "type": "array",
            "description": (
                "Functions whose real source you need to decide. Naming them here is "
                "the Pass C trigger — strongly preferred over guessing."
            ),
            "items": {"type": "string"},
        },
    },
}

PATH_VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": _PATH_VERDICT}},
}

# --- Pass D: adversarial refutation -----------------------------------------
# One verdict per verifier per finding. The prompt asks each to REFUTE, and the
# field is named `refuted` rather than `confirmed` on purpose: it makes the default
# answer under uncertainty ("I could not establish this") count against the
# finding, which is the direction that controls false positives.
REFUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["refuted", "confidence", "reason"],
    "properties": {
        "refuted": {
            "type": "boolean",
            "description": "TRUE if the finding does not hold up. Default TRUE when unsure.",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string", "description": "The specific thing that decided it."},
    },
}


def validate_verdicts(payload: dict, path_count: int) -> list[dict]:
    """Check a Pass B response. Verdict indices must reference paths we actually sent.

    Looser than Pass A's validation by design: a missing verdict costs one path's
    coverage, whereas a missing summary silently corrupts the store everything else
    reads. So an out-of-range index is rejected (it would attach a verdict to the
    wrong chain) while an omitted index is logged by the caller and skipped.
    """
    if not isinstance(payload, dict):
        raise ValidationError(f"expected an object, got {type(payload).__name__}")
    rows = payload.get("verdicts")
    if not isinstance(rows, list):
        raise ValidationError("missing 'verdicts' array")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("verdict entry is not an object")
        idx = row.get("path_index")
        if not isinstance(idx, int) or not (0 <= idx < path_count):
            raise ValidationError(
                f"path_index {idx!r} out of range for {path_count} path(s)"
            )
        if idx in seen:
            raise ValidationError(f"duplicate verdict for path_index {idx}")
        seen.add(idx)
    return rows


def unknown_callee_rate(rows: list[dict],
                        known_callees: dict[str, set[str]]) -> tuple[int, int]:
    """(claimed_callees_not_in_graph, total_claimed) across these summaries.

    Diagnostic, not a gate. A rate near zero means the model is reading the code
    it was given; a high rate means either it is inventing calls or the extractor
    is missing them — worth knowing which before trusting any of it."""
    unknown = total = 0
    for row in rows:
        known = known_callees.get(row.get("id", ""), set())
        for name in row.get("calls", []) or []:
            total += 1
            tail = str(name).rsplit(".", 1)[-1].split("(", 1)[0].strip()
            if tail and tail not in known:
                unknown += 1
    return unknown, total


# One object carrying every lens's verdict, so the panel is a single request instead
# of one per lens. `lenses` is an array rather than named keys because the lens set
# differs by finding kind, and a fixed-key object would need a schema per kind.
MULTI_REFUTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lenses"],
    "properties": {
        "lenses": {
            "type": "array",
            "description": (
                "One entry per lens you were given, in the order given. Answer each "
                "INDEPENDENTLY before drawing any overall conclusion — a lens that "
                "merely agrees with the previous one adds nothing."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["lens", "refuted", "confidence", "reason"],
                "properties": {
                    "lens": {"type": "string", "description": "The lens name given."},
                    "refuted": {
                        "type": "boolean",
                        "description": "TRUE if the finding fails under THIS lens. Default TRUE when unsure.",
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string", "description": "The specific thing that decided it."},
                },
            },
        },
    },
}

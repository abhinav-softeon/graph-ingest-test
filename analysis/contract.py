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

# THE TWO AXES SEVERITY IS COMPUTED FROM — and why severity itself is not a field
# any model fills in.
#
# Asking a model "how bad is this?" fails the same way as asking "is this function
# important?": it has no baseline to compare against, so it inflates, and the
# label cannot be re-tuned without paying for the whole repo again. The rule
# everywhere else in this pipeline is that the MODEL OBSERVES and the CODE JUDGES
# (see priority.py on risk.reasons); a model-assigned severity was the last place
# violating it.
#
# Both of these are answerable by someone reading the code. priority.severity()
# turns them into critical/high/medium/low, which means the rubric lives in a
# table that backfill_signals() can re-apply for free.
#
# It also collapses a real inconsistency: findings used to carry `confidence` and
# path verdicts carried `severity`, so the two could not be ranked against each
# other. CERTAINTY *is* confidence — one axis now does both jobs, for findings
# from every source.
CERTAINTY = ["demonstrated", "probable", "speculative"]
IMPACT = ["exposure", "integrity", "correctness", "quality"]

_CERTAINTY_DESC = (
    "How sure are you this is real? demonstrated - you can point at the code that "
    "does it. probable - the conditions look reachable but you have not shown it. "
    "speculative - you are inferring. Answer honestly; understating certainty is "
    "not penalised, and a later stage re-checks everything."
)
_IMPACT_DESC = (
    "What is at stake if it does happen. exposure - secrets, attacker-controlled "
    "data, or code execution. integrity - corrupted data, a security weakness, or "
    "a leaked resource. correctness - the code does the wrong thing. quality - it "
    "works and could be better: slow, duplicated, dead, unclear. `quality` is a "
    "normal and frequent answer, and never urgent."
)

_SUMMARY = {
    "type": "object",
    "additionalProperties": False,
    # EVERY FIELD HERE HAS A CONSUMER. Six were removed once path_pass began
    # reading real source, because each was answering a question something else
    # now answers better:
    #   params[].flows_to  the param -> callee-arg mapping existed so Pass B could
    #                      thread taint WITHOUT seeing code. path_pass reads the
    #                      bodies now and traces the parameter itself, with the
    #                      source in front of it.
    #   calls[]            the graph has every call at 99.98% from bytecode. This
    #                      was only ever a hallucination diagnostic
    #                      (unknown_callee_rate), not production data.
    #   returns            signature-level, already on the node.
    #   risk{}             almost entirely duplication: builds_sql_dynamically IS
    #                      db.sql_is_dynamic, manual_resource_handling IS
    #                      db.acquires, and reflection/deserialization/
    #                      spawns_process are all in touches[].
    #   uncertain[]        the source-expansion trigger, and that stage is gone.
    #   fields_read/written  measured: zero consumers anywhere in analysis/.
    # Output was ~1,480 tokens per function; these are the bulk of it.
    "required": ["id", "does", "contracts", "db", "touches", "source", "guards",
                 "findings"],
    "properties": {
        "id": {
            "type": "string",
            "description": "The exact function id given in the request. Never invent one.",
        },
        "does": {
            "type": "string",
            "description": "What this function does, one sentence. No preamble.",
        },
        # WHY THIS BLOCK IS SEPARATE FROM `returns`
        # `returns` is prose — useful in a report, useless in a query. These are the
        # same observations as booleans and name lists so Cypher can JOIN on them:
        # the callee's promise lives on the callee node, the caller's handling lives
        # on the caller node, and analysis/join.py matches the two across a CALLS
        # edge. Neither side can see the other (they are usually different files),
        # which is exactly why the graph has to be the meeting point.
        #
        # LISTS OF STRINGS, NOT JSON. Cypher cannot read into a JSON string — that
        # limitation is the entire reason priority.py exists — but it can test
        # membership in a list property. Anything here that a query must filter on
        # has to stay a scalar or a flat list.
        "contracts": {
            "type": "object",
            "additionalProperties": False,
            "required": ["may_return_null", "null_condition", "returns_sentinel",
                         "unguarded_calls", "swallowed_exception_calls"],
            "properties": {
                "may_return_null": {
                    "type": "boolean",
                    "description": (
                        "Can this function return null on ANY path? A bare `return "
                        "null;` anywhere in the body makes this TRUE, including on an "
                        "error or not-found branch. Nearly mechanical — read it off "
                        "the code, do not reason about whether callers cope."
                    ),
                },
                "null_condition": {
                    "type": "string",
                    "description": (
                        "When it returns null, in a few words ('when the key is "
                        "absent'). Empty string if may_return_null is false."
                    ),
                },
                "returns_sentinel": {
                    "type": "string",
                    "description": (
                        "A non-null failure value callers must check, e.g. '-1', '0', "
                        "'empty list', 'false'. Empty string if there is none. A "
                        "sentinel nobody checks is the same bug as an unchecked null."
                    ),
                },
                "unguarded_calls": {
                    "type": "array",
                    "description": (
                        "Method names called HERE whose return value this body uses "
                        "WITHOUT first checking it for null — dereferenced, passed on, "
                        "or returned. Bare method name as written, no class prefix. "
                        "Omit a call if the result is null-checked by any means: an if, "
                        "an early return, a ternary, or a validation helper. Omit calls "
                        "whose result is discarded. Empty is a common answer."
                    ),
                    "items": {"type": "string"},
                },
                "swallowed_exception_calls": {
                    "type": "array",
                    "description": (
                        "Method names whose exceptions this body catches and then "
                        "ignores — an empty catch, or one that only logs and continues "
                        "as if the call had succeeded. Empty if none."
                    ),
                    "items": {"type": "string"},
                },
            },
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
        "findings": {
            "type": "array",
            "description": (
                "Defects visible in THIS body alone — report every one you see, do not "
                "filter for importance. Empty is a valid and common answer."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "line", "detail", "certainty", "impact"],
                "properties": {
                    # WIDENED DELIBERATELY. A single-file read is the only pass that
                    # will ever look at most of this repo, and the marginal cost of one
                    # more check is zero once the file is in context — so the enum
                    # covers everything findable without leaving the file rather than
                    # only the classes the path analysis also chases.
                    #
                    # Kept as an ENUM rather than free text because priority.py has to
                    # rank on it and findings.py has to dedupe on it; 'other' is the
                    # escape hatch, but anything landing there repeatedly is a missing
                    # enum member, not a successful catch-all.
                    "kind": {
                        "type": "string",
                        "enum": [
                            # injection and untrusted input
                            "sql_injection", "command_injection", "path_traversal",
                            "deserialization", "xss", "xxe", "ssrf", "open_redirect",
                            "log_injection",
                            # secrets and crypto
                            "hardcoded_secret", "weak_crypto", "weak_random",
                            "tls_verification_disabled",
                            # session, auth, transport
                            "missing_authn", "missing_authz", "session_fixation",
                            "insecure_cookie", "sensitive_data_logged",
                            # resources and lifetime
                            "resource_leak",
                            # correctness and runtime
                            "null_dereference", "correctness", "concurrency",
                            "error_handling", "debug_code",
                            # NOT defects — code that works and could be better.
                            # These exist so an optimization has somewhere to go
                            # other than 'other'; impact='quality' keeps them from
                            # ever outranking a security finding.
                            "performance", "duplication", "dead_code", "complexity",
                            "other",
                        ],
                    },
                    "line": {"type": "integer", "description": "Absolute line number in the file."},
                    "detail": {"type": "string", "description": "What is wrong and why it matters."},
                    "certainty": {"type": "string", "enum": CERTAINTY,
                                  "description": _CERTAINTY_DESC},
                    "impact": {"type": "string", "enum": IMPACT,
                               "description": _IMPACT_DESC},
                },
            },
        },
    },
}

# --- wire shape: the same contract, two layers deep ------------------------
# Amazon Nova's constrained decoding guarantees valid JSON against the schema it
# is given, but AWS is explicit that schemas should be limited to TWO LAYERS OF
# NESTING for best performance, and warns that the smaller models struggle on
# large complex ones. _SUMMARY as authored is four deep:
#
#     root -> summaries[] -> summary -> db{} -> boolean
#
# Rather than maintain two hand-written schemas that would drift apart on the
# first edit, the nested definition above stays the single source of truth and
# the wire shape is DERIVED from it: the five grouping objects are spliced into
# prefixed scalars on the summary itself, taking it to exactly two layers.
#
#     db.released_in_finally  ->  db_released_in_finally
#     contracts.unguarded_calls -> contract_unguarded_calls
#
# `params` and `findings` stay arrays of objects — a third layer for those two
# only. They cannot be flattened without losing the per-item association that is
# their entire value, and the guidance is about performance rather than a limit.
#
# nest() puts the response back into the nested shape immediately on receipt, so
# priority.derive_signals, path_pass's renderer, single_file and every stored
# summary see exactly what they saw before. The flattening is a wire concern and
# stops at the edge.
_FLATTEN = {"db": "db_", "source": "src_",
            "guards": "guard_", "contracts": "contract_"}

# Prefixed name -> (block, original key). Built once; drives nest() so the two
# directions cannot disagree.
_FLAT_TO_NESTED: dict[str, tuple[str, str]] = {}


def _flatten_schema(nested: dict) -> dict:
    props: dict = {}
    required: list[str] = []
    for name in nested["required"]:
        spec = nested["properties"][name]
        prefix = _FLATTEN.get(name)
        if prefix is None:
            props[name] = spec
            required.append(name)
            continue
        for sub in spec["required"]:
            flat = f"{prefix}{sub}"
            props[flat] = spec["properties"][sub]
            required.append(flat)
            _FLAT_TO_NESTED[flat] = (name, sub)
    # Long free-text fields last, per AWS's tool-schema guidance: "place long
    # string arguments last in the schema and avoid nesting them".
    tail = [k for k in ("does", "contract_null_condition")
            if k in props]
    ordered = {k: v for k, v in props.items() if k not in tail}
    ordered.update({k: props[k] for k in tail})
    return {"type": "object", "additionalProperties": False,
            "required": required, "properties": ordered}


_SUMMARY_FLAT = _flatten_schema(_SUMMARY)


def nest(row: dict) -> dict:
    """Flat wire row -> the nested shape every consumer already expects.

    Unknown keys pass through untouched rather than being dropped: a model that
    returns something outside the schema is a bug worth seeing in validation, not
    one worth silently swallowing here.
    """
    out: dict = {}
    for key, value in row.items():
        target = _FLAT_TO_NESTED.get(key)
        if target is None:
            out[key] = value
            continue
        block, sub = target
        out.setdefault(block, {})[sub] = value
    return out


SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summaries"],
    "properties": {"summaries": {"type": "array", "items": _SUMMARY_FLAT}},
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
# 2 -> 3: added contracts{} (the join fields — may_return_null, unguarded_calls,
#         returns_sentinel, swallowed_exception_calls) and widened findings.kind
#         from 10 members to 25. The contracts block is the one that MUST force a
#         re-summarize: join.py returns nothing at all for a node that lacks it,
#         and "no contract mismatches" is indistinguishable from "never asked".
# 3 -> 4: findings[].confidence replaced by certainty{} + impact{}; severity is
#         now computed in priority.severity() rather than assigned by the model.
SCHEMA_VERSION = 5


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
    # Re-nest at the boundary. Everything past this line — the stored summary,
    # derive_signals, the path renderer — works on the shape it always did; only
    # the wire is flat. Checked AFTER nesting so _check_blocks keeps asking the
    # question it was written to ask.
    rows = [nest(row) for row in rows]
    _check_blocks(rows)
    return rows


# Blocks whose absence is silent rather than loud: derive_signals() defaults every
# one of them to "inert", so a response that omits them produces summaries that
# validate, store, and read as "nothing interesting here" for the whole repo.
_SIGNAL_BLOCKS = ("source", "guards", "db", "contracts")


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
    "required": ["path_index", "exploitable", "is_defect", "kind",
                 "certainty", "impact",
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
        # Same two axes as findings[], so a path verdict and a single-file
        # finding can be ranked against each other. path_pass computes `severity`
        # from these via priority.severity() before the finding is stored, which
        # is why findings.py and adversarial_pass still read a `severity` field
        # they never have to know is derived.
        "certainty": {"type": "string", "enum": CERTAINTY, "description": _CERTAINTY_DESC},
        "impact": {"type": "string", "enum": IMPACT, "description": _IMPACT_DESC},
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

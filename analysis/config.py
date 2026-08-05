"""Configuration for the analysis passes. Env-var driven, same style as graph_core.config.

MODEL IDS
Claude 3.5 Haiku (`claude-3-5-haiku-20241022`) was RETIRED on 2026-02-19 and returns
404 — `claude-haiku-4-5` replaces it. On Bedrock every Anthropic model id carries an
`anthropic.` prefix. Nova ids do not: Nova is an Amazon model served over Bedrock's
Converse API, a different request shape that the Anthropic SDK does not speak.

THINKING IS PER-MODEL AND NOT INTERCHANGEABLE
There is no single "enable thinking" field that works everywhere, so this module
renders it per model (see thinking_config). Getting it wrong is a 400, not a
degraded response:
  * claude-haiku-4-5 -> {"type": "enabled", "budget_tokens": N}
    budget_tokens must be >= 1024 AND strictly < max_tokens. `effort` ERRORS here.
  * claude-sonnet-5  -> {"type": "adaptive"}
    budget_tokens was REMOVED on this generation and returns 400. Depth is
    controlled by output_config.effort instead.

THREE BEDROCK LIMITATIONS THAT CHANGE THE COST MODEL
  * The Batch API does NOT exist on Bedrock — the 50%-off batch path is
    first-party Claude API only. Budget for full rate here.
  * Automatic prompt caching (top-level cache_control) is NOT on Bedrock either;
    only explicit cache_control on individual content blocks.
  * Haiku 4.5's minimum cacheable prefix is 4096 tokens. Pass A sends each file
    exactly once, so the only cacheable span is the system prompt — below 4096
    tokens it silently does not cache (cache_creation_input_tokens stays 0). The
    real saving in this design is body_hash skip-if-unchanged, not caching.
"""
from __future__ import annotations

import os

# --- models -----------------------------------------------------------------
# CROSS-REGION INFERENCE PROFILES. Bedrock ids may carry a geography prefix —
# `us.`, `eu.`, `apac.` — which routes the request across that geography's regions.
# So the provider segment is NOT always first: the real Haiku id is
# `us.anthropic.claude-haiku-4-5-...`, whose first segment is `us`, not `anthropic`.
# Anything matching on a leading "anthropic." would mis-route it to the Nova path.
# is_anthropic() below therefore looks for the provider segment anywhere.
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA_LITE = "us.amazon.nova-2-lite-v1:0"

_ANTHROPIC_SEGMENT = "anthropic."
_GEO_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")


def aws_region() -> str:
    """Region, accepting either env name.

    boto3 reads AWS_DEFAULT_REGION; the Anthropic Bedrock client takes an explicit
    aws_region. Both names are honored so one setting drives both clients."""
    for key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "us-east-1"


def summarizer_model() -> str:
    """Model for Pass A (bulk summarization). GRAPH_SUMMARIZER_MODEL to override.

    Haiku by default: Pass A is bounded, schema-constrained, single-file work with
    no cross-function reasoning — what a small model is good at. Every claim it
    makes is validated against the graph before being stored, so a weaker model
    costs recall on nuance, never correctness of the structural facts."""
    return os.environ.get("GRAPH_SUMMARIZER_MODEL", HAIKU).strip() or HAIKU


def adjudicator_model() -> str:
    """Model for Pass C/D (expansion + verdicts). GRAPH_ADJUDICATOR_MODEL to override.

    Defaults to the same Haiku as Pass A because that is what is actually available
    on this account. Note the tradeoff that creates: Pass D's adversarial panel is
    the step that controls false positives, and running it on the same model that
    produced the finding removes the independence the panel relies on. The diverse
    LENSES still make the verifiers fail differently, but if a stronger model becomes
    available, pointing GRAPH_ADJUDICATOR_MODEL at it is the single highest-value
    change to precision."""
    return os.environ.get("GRAPH_ADJUDICATOR_MODEL", HAIKU).strip() or HAIKU


def bedrock_client_mode() -> str:
    """Which Bedrock endpoint to use for Anthropic models: 'legacy' or 'mantle'.

    TWO DIFFERENT ENDPOINTS WITH DIFFERENT IAM ACTIONS, AND THAT IS THE WHOLE ISSUE
      legacy — bedrock-runtime InvokeModel. IAM action `bedrock:InvokeModel`.
               Model ids are long and versioned, with a geo prefix:
               us.anthropic.claude-haiku-4-5-20251001-v1:0
      mantle — the newer Messages-API endpoint. IAM action
               `bedrock-mantle:CreateInference`. Model ids are short:
               anthropic.claude-haiku-4-5

    Default is legacy, because an account provisioned for classic Bedrock has
    InvokeModel but usually NOT the Mantle action — that mismatch surfaces as a 403
    `permission_error` naming bedrock-mantle:CreateInference, which reads like a
    credentials problem and is actually an endpoint choice.

    The id format is the tell: a `-v1:0` suffix means classic/legacy. Sending a
    long versioned id to Mantle, or a short id to InvokeModel, fails independently
    of permissions.

    GRAPH_BEDROCK_CLIENT=mantle to switch."""
    mode = os.environ.get("GRAPH_BEDROCK_CLIENT", "legacy").strip().lower()
    return mode if mode in ("legacy", "mantle") else "legacy"


def structured_outputs_supported() -> bool:
    """Whether to send output_config.format (API-ENFORCED json_schema).

    The legacy InvokeModel path does not carry the newer request fields, so schema
    enforcement is unavailable there and JSON has to be prompt-requested and parsed
    defensively — the same posture as Nova. That is a real loss of guarantee, not a
    formatting detail: with enforcement a malformed summary is impossible, without
    it a malformed summary is a retry.

    Auto-derived from the client mode; GRAPH_STRUCTURED_OUTPUTS=1/0 to force."""
    forced = os.environ.get("GRAPH_STRUCTURED_OUTPUTS", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    return bedrock_client_mode() == "mantle"


def is_anthropic(model: str) -> bool:
    """Anthropic-on-Bedrock vs Nova — picks the client AND the request shape.

    Matches the provider segment anywhere, not just at the start, because a
    cross-region inference profile puts a geography first:
        anthropic.claude-haiku-4-5                     -> True
        us.anthropic.claude-haiku-4-5-20251001-v1:0    -> True  (geo-prefixed)
        us.amazon.nova-2-lite-v1:0                     -> False
    A leading-prefix check would send the geo-prefixed Haiku id down the Converse
    path, which fails in a way that looks like a credentials or model-access problem
    rather than a routing bug."""
    return _ANTHROPIC_SEGMENT in model


def max_output_tokens() -> int:
    """Non-streaming cap. 16000 keeps requests under SDK HTTP timeouts; raise this
    only alongside streaming."""
    try:
        v = int(os.environ.get("GRAPH_LLM_MAX_TOKENS", "16000"))
    except ValueError:
        return 16000
    return v if v > 0 else 16000


def thinking_budget() -> int:
    """Thinking tokens for models that take an explicit budget (Haiku 4.5).

    Clamped to [1024, max_output_tokens - 1024]: the API floor is 1024 and the
    budget must stay strictly below max_tokens, so a budget that crowds out the
    answer is a truncated response rather than an error. Ignored by models on
    adaptive thinking, which reject budget_tokens outright."""
    try:
        v = int(os.environ.get("GRAPH_THINKING_BUDGET", "4000"))
    except ValueError:
        v = 4000
    ceiling = max(1024, max_output_tokens() - 1024)
    return max(1024, min(v, ceiling))


def thinking_budget_for(pass_name: str = "") -> int:
    """Thinking budget for a specific pass. GRAPH_THINKING_BUDGET_<PASS> overrides.

    PER-PASS BECAUSE THINKING IS 77% OF THE OUTPUT BILL, MEASURED
    Pass A emits 3,260 output tokens per call of which only 754 is the JSON summary —
    the other 2,506 are reasoning tokens, billed as output and discarded. That pass
    runs once per FILE, so at repo scale it is the largest single line item in the
    system, and it is schema-bound extraction rather than judgment.

    The passes that genuinely reason — adjudication, source expansion — run orders of
    magnitude fewer times, so depth there is nearly free. One global budget forced the
    opposite allocation: maximum thinking on the highest-volume, least-analytical pass.
    """
    try:
        default = int(os.environ.get("GRAPH_THINKING_BUDGET", "4000"))
    except ValueError:
        default = 4000
    if pass_name:
        raw = os.environ.get(f"GRAPH_THINKING_BUDGET_{pass_name.upper()}")
        if raw is not None:
            try:
                default = int(raw)
            except ValueError:
                pass
    ceiling = max(1024, max_output_tokens() - 1024)
    return max(1024, min(default, ceiling))


def thinking_config(model: str, pass_name: str = "") -> dict | None:
    """The `thinking` request field for this model, or None if it takes none.

    This is the whole reason thinking config is centralised: the two Anthropic
    generations in play disagree, and sending the wrong one is a 400.
    """
    if not is_anthropic(model):
        # Nova: reasoning is configured through Converse's
        # additionalModelRequestFields, NOT a `thinking` field. Shape unverified —
        # handled in llm.py, never here.
        return None
    if "haiku-4-5" in model:
        return {"type": "enabled", "budget_tokens": thinking_budget_for(pass_name)}
    # Sonnet 5 / Opus 5 generation: adaptive only, budget_tokens rejected.
    return {"type": "adaptive"}


def effort_config(model: str) -> dict | None:
    """`output_config` for this model, or None.

    `effort` ERRORS on Haiku 4.5 — it is not merely ignored — so it is only ever
    sent to the generation that supports it."""
    if not is_anthropic(model) or "haiku-4-5" in model:
        return None
    return {"effort": os.environ.get("GRAPH_LLM_EFFORT", "high").strip() or "high"}


def max_functions_per_call() -> int:
    """Functions requested per LLM call (default 20).

    The whole file is sent as context either way; this caps how many summaries one
    response must produce, because structured output degrades toward the end of a
    long response. A 60-method class is chunked, and the file text is re-sent per
    chunk — the one place this design pays to see code twice. Keep it high enough
    that most files stay a single call."""
    try:
        v = int(os.environ.get("GRAPH_MAX_FUNCTIONS_PER_CALL", "20"))
    except ValueError:
        return 20
    return v if v > 0 else 20


def llm_workers() -> int:
    """Concurrent LLM calls, shared by all four passes.

    Every pass is embarrassingly parallel — Pass A summaries are independent by
    design (no callee dependency, no fixpoint), Pass B batches don't interact, Pass C
    expansions are per-finding, and Pass D's lens votes are deliberately independent.
    So this knob scales throughput almost linearly until Bedrock starts throttling.

    RAISING THIS IS ONLY SAFE BECAUSE OF llm_max_retries(). Without retry a throttled
    call returns an error result, which Pass A records as a rejected chunk — the
    summaries are simply missing, and the run still reports success. More workers
    would buy speed by losing data.
    """
    try:
        v = int(os.environ.get("GRAPH_LLM_WORKERS", "12"))
    except ValueError:
        return 12
    return v if v > 0 else 12


def llm_max_retries() -> int:
    """Retries for throttling and transient 5xx, handled inside the SDK.

    The Anthropic SDK and botocore both retry with exponential backoff internally,
    which is strictly better than a retry loop here: they know which status codes are
    retryable, they honor Retry-After, and they do not re-send a request the server
    already accepted. This just raises their default (2) because concurrency at the
    level above makes brief throttling normal rather than exceptional.
    """
    try:
        v = int(os.environ.get("GRAPH_LLM_MAX_RETRIES", "6"))
    except ValueError:
        return 6
    return max(0, v)


def nova_reasoning(pass_name: str = "") -> dict | None:
    """Nova 2 Lite `reasoningConfig`, or None to leave extended thinking off.

    VERIFIED SHAPE (probed against us.amazon.nova-2-lite-v1:0):
        {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low|medium|high"}}
    BOTH keys are required — sending `type` alone is rejected with
    "extraneous key [reasoningConfig] is not permitted", which reads like the whole
    field is unsupported and is actually a missing sub-key.

    EFFORT IS PER-PASS BECAUSE HIGH IS EXTRAORDINARILY EXPENSIVE ON VOLUME
    Measured on one trivial question: low returned 130 output tokens in 2.0s, high
    returned 4,289 in 40.0s — 33x the tokens and 20x the latency for the same
    one-sentence answer. Reasoning bills as output. So high effort belongs on the
    passes with few calls and hard judgments (adjudication), never on the
    per-file pass that runs once per file in the repo.

    GRAPH_NOVA_EFFORT sets the default; GRAPH_NOVA_EFFORT_PASS_A etc. override it.
    Set to 'off' to disable extended thinking for that pass.
    """
    default = os.environ.get("GRAPH_NOVA_EFFORT", "low").strip().lower()
    if pass_name:
        key = f"GRAPH_NOVA_EFFORT_{pass_name.upper()}"
        default = os.environ.get(key, default).strip().lower()
    if default in ("off", "none", "disabled", ""):
        return None
    if default not in ("low", "medium", "high"):
        default = "low"
    return {"reasoningConfig": {"type": "enabled", "maxReasoningEffort": default}}


def nova_effort_is_high(pass_name: str = "") -> bool:
    """True when this pass runs Nova at high effort.

    Callers need this because high effort REJECTS temperature, topP and maxTokens —
    the request must omit inferenceConfig entirely or it errors. That constraint is
    documented for the model, not inferable from the response."""
    cfg = nova_reasoning(pass_name)
    return bool(cfg) and cfg["reasoningConfig"]["maxReasoningEffort"] == "high"

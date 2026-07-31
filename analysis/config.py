"""Configuration for the analysis passes. Env-var driven, same style as graph_core.config.

MODEL CHOICE, AND WHY THESE IDS
Claude 3.5 Haiku (`claude-3-5-haiku-20241022`) was RETIRED on 2026-02-19 and returns
404. `claude-haiku-4-5` replaces it. On Bedrock every Anthropic model id carries an
`anthropic.` prefix; Nova ids do not (they are Amazon models on a different API).

THREE BEDROCK LIMITATIONS THAT CHANGE THE COST MODEL
  * The Batch API does NOT exist on Bedrock. The 50%-off batch path is first-party
    Claude API only. Budget for full rate here.
  * Automatic prompt caching (top-level cache_control) is NOT on Bedrock either —
    only explicit cache_control blocks on individual content blocks.
  * Haiku 4.5's minimum cacheable prefix is 4096 tokens. Since Pass A sends each
    file exactly once, the only cacheable span is the system prompt; below 4096
    tokens it silently does not cache (cache_creation_input_tokens stays 0). The
    real saving in this design is body_hash skip-if-unchanged, not caching.
"""
from __future__ import annotations

import os

# --- models -----------------------------------------------------------------
# Bedrock ids. Anthropic models take the `anthropic.` prefix; Nova does not.
HAIKU = "anthropic.claude-haiku-4-5"
NOVA_LITE = "amazon.nova-lite-v1:0"
# Adjudication tier — a wrong call here costs a missed vulnerability, so this is
# deliberately not Haiku. Only Pass C/D use it.
SONNET = "anthropic.claude-sonnet-5"


def aws_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"


def summarizer_model() -> str:
    """Model for Pass A (bulk summarization). GRAPH_SUMMARIZER_MODEL to override.

    Haiku by default: Pass A is bounded, schema-constrained, single-file work with
    no cross-function reasoning — exactly what a small model is good at. Every
    claim it makes is validated against the graph before being stored."""
    return os.environ.get("GRAPH_SUMMARIZER_MODEL", HAIKU).strip() or HAIKU


def adjudicator_model() -> str:
    """Model for Pass C/D (expansion + verdicts). GRAPH_ADJUDICATOR_MODEL to override."""
    return os.environ.get("GRAPH_ADJUDICATOR_MODEL", SONNET).strip() or SONNET


def max_functions_per_call() -> int:
    """Functions requested per LLM call (default 20).

    The whole file is sent as context either way — this caps how many summaries
    one response must produce. Output quality degrades toward the end of a long
    structured response, so a 60-method class is chunked. The file text is re-sent
    per chunk, which is the one place this design pays to see code twice; keep the
    number high enough that most files are a single call."""
    try:
        v = int(os.environ.get("GRAPH_MAX_FUNCTIONS_PER_CALL", "20"))
    except ValueError:
        return 20
    return v if v > 0 else 20


def max_output_tokens() -> int:
    """Non-streaming cap. 16000 keeps requests under SDK HTTP timeouts; raise only
    with streaming (see the claude-api guidance on max_tokens)."""
    try:
        v = int(os.environ.get("GRAPH_LLM_MAX_TOKENS", "16000"))
    except ValueError:
        return 16000
    return v if v > 0 else 16000


def llm_workers() -> int:
    """Concurrent LLM calls. Pass A is embarrassingly parallel — summaries are
    independent by design (no callee dependency, so no fixpoint and no ordering)."""
    try:
        v = int(os.environ.get("GRAPH_LLM_WORKERS", "6"))
    except ValueError:
        return 6
    return v if v > 0 else 6

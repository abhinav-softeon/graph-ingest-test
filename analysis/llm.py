"""LLM clients for the analysis passes. Two providers, one interface.

WHY TWO CLIENTS AND NOT ONE
Anthropic-on-Bedrock and Nova-on-Bedrock are different APIs, not two model ids on
one API. Anthropic models are reached through the Anthropic SDK's Bedrock client
(Messages shape: `system` + `messages` + `thinking` + `output_config`); Nova is
reached through boto3's `bedrock-runtime.converse` (Converse shape: `system` +
`messages` + `inferenceConfig` + `additionalModelRequestFields`). Neither speaks
the other's request body, so a single client class would be a lie.

THINKING IS ON FOR BOTH, BY DIFFERENT MEANS
  * Haiku 4.5 -> thinking={"type": "enabled", "budget_tokens": N}
  * Sonnet 5  -> thinking={"type": "adaptive"} + output_config.effort
  * Nova      -> Converse additionalModelRequestFields (SHAPE UNVERIFIED, see below)
config.thinking_config()/effort_config() own that dispatch so no call site has to.

STRUCTURED OUTPUT DIFFERS TOO, AND THAT MATTERS FOR TRUST
Anthropic models get `output_config.format` with a json_schema, which the API
ENFORCES — the response is valid against the schema or the request fails. It is
compatible with extended thinking, so Pass A gets thinking *and* guaranteed shape.
Nova has no equivalent enforcement here, so its JSON is prompt-requested and
parsed defensively. That asymmetry is why the summarizer defaults to Haiku: on
Nova a malformed response is a retry, on Haiku it cannot happen.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import config
from sail_core.logger.logger import get_logger

_log = get_logger(__name__)


@dataclass
class LLMResult:
    text: str = ""
    parsed: dict | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Zero across repeated calls means nothing cached. On Haiku 4.5 the minimum
    # cacheable prefix is 4096 tokens, so a shorter system prompt silently never
    # caches — this field is how you find that out rather than assuming.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""
    stop_reason: str = ""
    seconds: float = 0.0
    error: str = ""
    # Distinguishes "we asked for more concurrency than the account allows" from
    # "the request was wrong". Counted per pass so a run that lost work to quota
    # says so, instead of reporting a lower finding count as if it were the answer.
    throttled: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text)


# Matched on the exception's text rather than its class because the same condition
# arrives as three unrelated types depending on the path: anthropic.RateLimitError
# from the SDK, botocore ThrottlingException from the signer underneath it, and
# ModelNotReadyException from Bedrock itself under load. Importing all three to
# isinstance-check them would make optional dependencies mandatory.
_THROTTLE_MARKERS = ("throttl", "too many requests", "429", "rate limit",
                     "modelnotready", "serviceunavailable", "slow down")


def _is_throttle(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    return any(m in str(exc).lower() for m in _THROTTLE_MARKERS)


def extract_json(text: str) -> dict | None:
    """Best-effort JSON object out of a model response.

    Only needed for providers without enforced structured output (Nova). Tries the
    whole string, then a fenced block, then the outermost brace span — in that
    order, so a clean response never pays for the fallbacks."""
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _json_candidates(text: str):
    yield text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        yield fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        yield text[start:end + 1]


class ClaudeBedrock:
    """Anthropic models on Bedrock. Two endpoints, chosen by config.

    `AnthropicBedrock` is the classic bedrock-runtime InvokeModel path and needs
    the IAM action `bedrock:InvokeModel`. `AnthropicBedrockMantle` is the newer
    Messages-API endpoint and needs `bedrock-mantle:CreateInference` — an account
    provisioned for classic Bedrock typically has the first and not the second, so
    legacy is the default. See config.bedrock_client_mode.

    The two also differ in what the request body may carry: `output_config.format`
    (enforced JSON schema) is Mantle-only, so on legacy the schema is requested in
    the prompt and the response is parsed defensively instead.
    """

    def __init__(self, model: str, region: str | None = None, pass_name: str = ""):
        self.model = model
        self.pass_name = pass_name
        self.mode = config.bedrock_client_mode()
        region = region or config.aws_region()
        # max_retries is what makes raising GRAPH_LLM_WORKERS safe. The SDK retries
        # 429/5xx with exponential backoff and honors Retry-After; without it a
        # throttled call returns an error result and Pass A records a rejected chunk,
        # so the summaries go missing while the run still reports success.
        retries = config.llm_max_retries()
        if self.mode == "mantle":
            from anthropic import AnthropicBedrockMantle  # lazy: optional dependency
            self._client = AnthropicBedrockMantle(aws_region=region, max_retries=retries)
        else:
            from anthropic import AnthropicBedrock  # lazy: optional dependency
            self._client = AnthropicBedrock(aws_region=region, max_retries=retries)

    def complete(self, system: str, user: str, schema: dict | None = None) -> LLMResult:
        t0 = time.monotonic()
        enforce = schema is not None and config.structured_outputs_supported()
        if schema is not None and not enforce:
            # No enforcement available on this endpoint, so ask for the shape and
            # validate after. Every caller already re-validates against the graph,
            # so the loss is a retry on malformed JSON, not a bad summary stored.
            user = (f"{user}\n\nReturn ONLY a JSON object matching this schema, "
                    f"with no prose and no code fence:\n{json.dumps(schema)}")
        kwargs: dict = {
            "model": self.model,
            "max_tokens": config.max_output_tokens(),
            # cache_control on the block, not top-level: automatic caching is not
            # available on Bedrock. Whether it actually caches depends on the
            # system prompt clearing the model's minimum prefix — check
            # cache_write_tokens on the first call rather than assuming.
            "system": [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user}],
        }
        thinking = config.thinking_config(self.model, self.pass_name)
        if thinking:
            kwargs["thinking"] = thinking
        output_config = config.effort_config(self.model) or {}
        if enforce:
            # Enforced by the API, and compatible with extended thinking — so a
            # response that parses is also guaranteed to match the schema.
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config

        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the pass
            # Throttling is reported distinctly from other failures because it means
            # something different: not a bug, but GRAPH_LLM_WORKERS set above what
            # this account's quota supports. Reaching here at all means the SDK
            # already exhausted max_retries, so the work IS lost — the fix is fewer
            # workers, and that is only actionable if the log says which it was.
            if _is_throttle(exc):
                _log.warning(
                    "[llm][%s] THROTTLED after %s retries — lower GRAPH_LLM_WORKERS "
                    "(currently %s); this call's work is lost",
                    self.model, config.llm_max_retries(), config.llm_workers())
                return LLMResult(model=self.model, error=f"throttled: {exc}",
                                 throttled=True, seconds=time.monotonic() - t0)
            _log.warning("[llm][%s] request failed: %s", self.model, exc)
            return LLMResult(model=self.model, error=str(exc),
                             seconds=time.monotonic() - t0)

        # A refusal returns HTTP 200 with empty/partial content — reading
        # content[0] unconditionally would raise here rather than report.
        if getattr(resp, "stop_reason", "") == "refusal":
            _log.warning("[llm][%s] request refused by safety classifiers", self.model)
            return LLMResult(model=self.model, stop_reason="refusal",
                             error="refusal", seconds=time.monotonic() - t0)

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        usage = resp.usage
        out = LLMResult(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            model=getattr(resp, "model", self.model),
            stop_reason=getattr(resp, "stop_reason", "") or "",
            seconds=time.monotonic() - t0,
        )
        if out.stop_reason == "max_tokens":
            # Thinking and the answer share max_tokens, so a large thinking budget
            # can starve the response. Surfaced rather than silently truncated.
            _log.warning(
                "[llm][%s] hit max_tokens (%s output) — raise GRAPH_LLM_MAX_TOKENS "
                "or lower GRAPH_THINKING_BUDGET", self.model, out.output_tokens,
            )
        out.parsed = extract_json(text) if text else None
        return out


class NovaBedrock:
    """Amazon Nova on Bedrock, via boto3 Converse.

    ⚠️ REASONING CONFIG IS UNVERIFIED. Nova exposes model-specific knobs through
    Converse's `additionalModelRequestFields`, and the exact key for Nova Lite 2
    reasoning has NOT been confirmed against current AWS documentation. It is read
    from GRAPH_NOVA_REASONING_JSON so it can be corrected without a code change,
    and it defaults to EMPTY — meaning no reasoning is requested until you supply
    a verified shape. A guessed key here is worse than none: Converse may accept
    and ignore an unknown field, which looks exactly like reasoning being on.

    Verify with a one-off call and check whether the response carries a reasoning
    block before trusting this path for anything.
    """

    def __init__(self, model: str, region: str | None = None, pass_name: str = ""):
        import boto3  # lazy: optional dependency
        from botocore.config import Config as BotoConfig

        self.model = model
        # Which pass this client serves, so reasoning effort can differ per pass.
        self.pass_name = pass_name
        # 'adaptive' rather than 'standard': it adds a client-side rate limiter that
        # slows down BEFORE hitting the quota, instead of only backing off after a
        # 429. With many workers sharing one account that difference is the whole
        # point — standard mode lets all of them collide, then all back off together.
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region or config.aws_region(),
            config=BotoConfig(retries={"max_attempts": config.llm_max_retries(),
                                       "mode": "adaptive"}),
        )

    def _additional_fields(self) -> dict:
        import os
        raw = os.environ.get("GRAPH_NOVA_REASONING_JSON", "").strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            _log.warning("[llm][nova] GRAPH_NOVA_REASONING_JSON is not valid JSON — ignored")
            return {}

    def complete(self, system: str, user: str, schema: dict | None = None) -> LLMResult:
        t0 = time.monotonic()
        # No enforced structured output on this path, so the schema is requested in
        # the prompt and the response is parsed defensively.
        if schema:
            user = (f"{user}\n\nReturn ONLY a JSON object matching this schema, "
                    f"with no prose and no code fence:\n{json.dumps(schema)}")
        req: dict = {
            "modelId": self.model,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
        }
        extra = config.nova_reasoning(self.pass_name) or self._additional_fields()
        if extra:
            req["additionalModelRequestFields"] = extra
        # At high effort Nova REJECTS maxTokens/temperature/topP outright, so the
        # whole inferenceConfig has to be omitted rather than trimmed. It is also
        # why high effort can return >65k tokens: nothing caps it.
        if not config.nova_effort_is_high(self.pass_name):
            req["inferenceConfig"] = {"maxTokens": config.max_output_tokens()}

        try:
            resp = self._client.converse(**req)
        except Exception as exc:  # noqa: BLE001
            if _is_throttle(exc):
                _log.warning("[llm][%s] THROTTLED after %s attempts — lower "
                             "GRAPH_LLM_WORKERS (currently %s)",
                             self.model, config.llm_max_retries(), config.llm_workers())
                return LLMResult(model=self.model, error=f"throttled: {exc}",
                                 throttled=True, seconds=time.monotonic() - t0)
            _log.warning("[llm][%s] converse failed: %s", self.model, exc)
            return LLMResult(model=self.model, error=str(exc),
                             seconds=time.monotonic() - t0)

        blocks = resp.get("output", {}).get("message", {}).get("content", []) or []
        text = "".join(b.get("text", "") for b in blocks if "text" in b)
        usage = resp.get("usage", {}) or {}
        out = LLMResult(
            text=text,
            input_tokens=usage.get("inputTokens", 0) or 0,
            output_tokens=usage.get("outputTokens", 0) or 0,
            model=self.model,
            stop_reason=resp.get("stopReason", "") or "",
            seconds=time.monotonic() - t0,
        )
        out.parsed = extract_json(text) if text else None
        if schema and out.parsed is None and text:
            out.error = "response did not contain parseable JSON"
            _log.warning("[llm][%s] unparseable JSON response (%s chars)",
                         self.model, len(text))
        return out


def get_client(model: str, region: str | None = None, pass_name: str = ""):
    """Client for a model id. Anthropic ids take the `anthropic.` prefix; anything
    else is routed to the Nova/Converse path."""
    if config.is_anthropic(model):
        return ClaudeBedrock(model, region, pass_name)
    return NovaBedrock(model, region, pass_name)

"""LLM client. The ONLY place in ASHS where a model provider appears.

Two calls per episode, both deliberately narrow:

  1. Diagnosis narrative -- evidence in, root-cause explanation out.
  2. Patch generation    -- evidence plus source in, unified diff plus test out.

The model NEVER decides ownership, routing, or policy. Those are deterministic
(see attribution.py and policy.py). This separation is the entire credibility
argument: the model is strongest at interpretation and plan generation, and the
platform stays deterministic for identity, permissions, policy and verification.

DEGRADED MODE IS A FIRST-CLASS PATH. If no credentials are configured, or the
provider is unreachable, `available()` returns False and every call returns
None. The caller escalates with the deterministic diagnosis intact rather than
stalling. That is also the live-demo safety net: attribution, policy and the
test gate never call a model, so a dead network cannot take the demo down.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)

PROVIDER = os.environ.get("LLM_PROVIDER", "bedrock").lower()
# Bedrock model ids carry an `anthropic.` prefix; the first-party API does not.
MODEL = os.environ.get("LLM_MODEL", "anthropic.claude-opus-5")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
EFFORT = os.environ.get("LLM_EFFORT", "high")
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8000"))

_client = None
_init_error: str | None = None
_last_check = 0.0


class LLMUnavailable(RuntimeError):
    """Raised internally when no provider is usable. Callers degrade, not crash."""


def _build_client():
    """Construct the provider client.

    Bedrock resolves credentials itself through the standard AWS chain --
    env vars, then ~/.aws/credentials, then assumed role, then instance
    metadata. There is deliberately no credential handling code here.
    """
    global _init_error

    if PROVIDER == "bedrock":
        if not (os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")):
            _init_error = "no AWS credentials in the environment"
            raise LLMUnavailable(_init_error)
        from anthropic import AnthropicBedrockMantle

        return AnthropicBedrockMantle(aws_region=AWS_REGION)

    if PROVIDER == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            _init_error = "ANTHROPIC_API_KEY is not set"
            raise LLMUnavailable(_init_error)
        from anthropic import Anthropic

        return Anthropic()

    _init_error = f"unknown LLM_PROVIDER '{PROVIDER}'"
    raise LLMUnavailable(_init_error)


def client():
    global _client
    if _client is None:
        _client = _build_client()
    return _client


_last_failure: str | None = None


def note_failure(detail: str) -> None:
    """Record why the last call failed, so callers can report it honestly."""
    global _last_failure
    _last_failure = detail or None


def available() -> tuple[bool, str]:
    """Can we construct a client, and did the last call succeed?

    Constructing a client proves almost nothing: EXPIRED CREDENTIALS CONSTRUCT
    FINE and only fail at call time. An earlier version reported "available"
    and then degraded anyway, logging the model id as if it were the reason --
    which is worse than useless when you are trying to work out why a demo
    stopped repairing things. The last observed failure is carried forward so
    the audit trail names the real cause.
    """
    global _last_check, _init_error
    try:
        client()
        _init_error = None
        _last_check = time.time()
    except Exception as exc:  # noqa: BLE001 - unavailability is an expected state
        _init_error = str(exc)
        return False, _init_error

    if _last_failure:
        return False, _last_failure
    return True, f"{PROVIDER}:{MODEL}"


def status() -> dict:
    ok, detail = available()
    return {
        "provider": PROVIDER,
        "model": MODEL,
        "region": AWS_REGION if PROVIDER == "bedrock" else None,
        "effort": EFFORT,
        "available": ok,
        "detail": detail,
        "degraded_behaviour": (
            "Episodes escalate with the deterministic diagnosis intact. "
            "Attribution, policy and the test gate never call a model."
        ),
    }


def complete_json(system: str, user: str, schema: dict, max_tokens: int | None = None) -> dict | None:
    """One structured call. Returns parsed JSON, or None if degraded.

    STRUCTURE COMES FROM FORCED TOOL USE, NOT `output_config.format`.

    That is not a stylistic choice -- it is what this endpoint accepts. Probed
    against Bedrock (eu-west-1, anthropic.claude-opus-5):

        adaptive thinking        OK
        output_config.effort     OK
        output_config.format     400  "Extra inputs are not permitted"
        tools[].strict           400  "Extra inputs are not permitted"
        forced non-strict tools  OK   <- what we use

    So a single tool is declared with the desired schema as its input schema and
    `tool_choice` forces it. The model's arguments ARE the structured payload,
    which is more reliable than asking for JSON in prose and parsing it back.

    Also on Opus 5: temperature / top_p / top_k are removed and return 400.
    Steer with the prompt, never with sampling parameters.
    """
    try:
        c = client()
    except LLMUnavailable as exc:
        log.warning("LLM unavailable, degrading: %s", exc)
        return None

    tool_name = "emit_result"
    started = time.time()
    try:
        response = c.messages.create(
            model=MODEL,
            max_tokens=max_tokens or MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            tools=[{
                "name": tool_name,
                "description": "Emit the structured result. Call this exactly once.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": tool_name},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 - any provider failure degrades
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        if "expired" in str(exc).lower() or "401" in str(exc):
            detail = (
                "AWS credentials have EXPIRED. STS/SSO session tokens last "
                "1-12h -- re-export them into ./.env and run `make restart-control`. "
                f"({type(exc).__name__})"
            )
        note_failure(detail)
        log.error("LLM call failed after %.1fs: %s", time.time() - started, detail)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        log.warning("LLM refused: %s", getattr(details, "category", "unknown"))
        return None

    block = next((b for b in response.content if b.type == "tool_use"), None)
    if block is None:
        # Fall back to a text block carrying JSON, in case a future provider
        # returns prose despite the forced tool.
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            log.warning("LLM returned no tool_use and no text (stop=%s)", response.stop_reason)
            return None
        try:
            parsed = json.loads(text.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
        except json.JSONDecodeError:
            log.warning("LLM returned neither a tool call nor parseable JSON")
            return None
    else:
        parsed = dict(block.input)

    note_failure("")  # a successful call clears the sticky failure state
    usage = getattr(response, "usage", None)
    parsed["_meta"] = {
        "model": getattr(response, "model", MODEL),
        "provider": PROVIDER,
        "effort": EFFORT,
        "latency_ms": round((time.time() - started) * 1000),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    return parsed

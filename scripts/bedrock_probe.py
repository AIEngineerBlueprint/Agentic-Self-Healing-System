#!/usr/bin/env python3
"""Runs INSIDE the control container. Makes one real Bedrock call."""
import sys
sys.path.insert(0, "/app/src")
from control import llm

ok, detail = llm.available()
print(f"available: {ok}  ({detail})")
if not ok:
    sys.exit(1)

schema = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confident": {"type": "boolean"}},
    "required": ["answer", "confident"],
    "additionalProperties": False,
}
# Call the provider directly so the REAL error is reported here rather than
# buried in a container log. "see the logs" is not a diagnosis.
try:
    c = llm.client()
    resp = c.messages.create(
        model=llm.MODEL, max_tokens=2000,
        thinking={"type": "adaptive"},
        output_config={"effort": llm.EFFORT},
        tools=[{"name": "emit_result", "description": "Emit the result.",
                "input_schema": schema}],
        tool_choice={"type": "tool", "name": "emit_result"},
        system="You are verifying connectivity. Answer in one short sentence.",
        messages=[{"role": "user",
                   "content": "Reply with the exact word OK as the answer, and true for confident."}],
    )
except Exception as exc:  # noqa: BLE001
    msg = str(exc)
    print(f"\ncall FAILED: {type(exc).__name__}")
    if "expired" in msg.lower() or "401" in msg:
        print("\n  AWS CREDENTIALS HAVE EXPIRED.")
        print("  STS/SSO session tokens last 1-12 hours.")
        print("  Re-export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /")
        print("  AWS_SESSION_TOKEN into ./.env, then: make restart-control")
        print("\n  For a demo machine, a long-lived IAM user key scoped to")
        print("  bedrock:InvokeModel avoids this entirely.")
    else:
        print(f"  {msg[:400]}")
    sys.exit(2)

block = next((b for b in resp.content if b.type == "tool_use"), None)
out = dict(block.input) if block else None
if out is None:
    print("call returned no structured result")
    sys.exit(2)
out["_meta"] = {"model": resp.model,
                "latency_ms": None,
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens}

meta = out.pop("_meta", {})
print(f"response : {out}")
print(f"model    : {meta.get('model')}")
print(f"latency  : {meta.get('latency_ms')} ms")
print(f"tokens   : in={meta.get('input_tokens')} out={meta.get('output_tokens')}")
print("BEDROCK OK -- structured output round-tripped")

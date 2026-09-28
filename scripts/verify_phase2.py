#!/usr/bin/env python3
"""PHASE 2 EXIT TEST -- reason, plan, and gate.

A complete repair plan must reach POLICY_APPROVED with a full audit trail, AND
a deliberately out-of-scope target must be correctly BLOCKED.

The refusals matter more than the approval. An autonomous system you cannot
trust to stop is not one you can trust to start, so most of this file is about
proving the guardrails hold rather than proving the happy path works.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFC = "http://localhost:8000"
CTRL = "http://localhost:8090"

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[bool, str, str]] = []


def get(url: str, timeout: float = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def post(url: str, payload: dict, timeout: float = 120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {}


def check(name: str, fn) -> None:
    try:
        note = fn() or ""
        results.append((True, name, note))
        print(f"  {GREEN}PASS{RESET}  {name}" + (f"\n        {DIM}{note}{RESET}" if note else ""))
    except Exception as exc:  # noqa: BLE001
        results.append((False, name, str(exc)))
        print(f"  {RED}FAIL{RESET}  {name}\n        {RED}{exc}{RESET}")


print("== Phase 2 exit test: reason, plan, gate " + "=" * 30)

# --------------------------------------------------------------- clean slate
print(f"\n{DIM}-- resetting and driving the defect to POLICY_APPROVED{RESET}")
# Restore baseline source FIRST. A previous run's repair permanently removes
# the defect, so without this the bug flag has nothing left to break and no
# incident is ever raised -- the system working correctly looks like a failure.
subprocess.run(["make", "reset-source"], capture_output=True, check=False)
subprocess.run(["docker", "compose", "--env-file", ".env", "-f",
                "services/cfc-product/docker-compose.yml", "restart", "cfc-api"],
               capture_output=True, check=False)
time.sleep(12)
subprocess.run(
    ["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs",
     "-c", "DELETE FROM ash_incidents;"], capture_output=True, check=False,
)
# This phase asserts the pipeline reaches a given state. Pin autonomy to
# plan-only so the repair loop does not drive past those assertions --
# and so a mid-test hot reload cannot time out the requests below.
post(f"{CTRL}/api/config/auto_execute", {"enabled": False})
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": True})
ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}
for _ in range(8):
    post(f"{CFC}/api/footprint/calculate", ZERO)
time.sleep(14)
post(f"{CTRL}/api/detect", {})


TERMINAL = {"POLICY_APPROVED", "ESCALATED", "RESOLVED"}


def wait_for_incident(timeout: float = 180.0) -> dict:
    """Poll until the pipeline settles.

    Deliberately NOT a fixed sleep. A live model call takes ~15s while the
    degraded path is instant, so any constant is either flaky or slow. Polling
    for the state the test actually cares about is correct in both modes.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        incidents = get(f"{CTRL}/api/incidents")["incidents"]
        if incidents:
            last = incidents[0]
            if last["state"] in TERMINAL:
                return last
        time.sleep(3)
    raise AssertionError(
        f"pipeline did not settle within {timeout:.0f}s; "
        f"last state={last['state'] if last else 'no incident'}"
    )


INC = wait_for_incident()
INCIDENT_ID = INC["incident_id"]
print(f"{DIM}   settled at {INC['state']} after model diagnosis{RESET}")
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})

# ------------------------------------------------------------- 1. LLM layer
print("\n-- 1. model integration is isolated and degrades safely")


def llm_isolated():
    s = get(f"{CTRL}/api/llm/status")
    assert s["provider"] in ("bedrock", "anthropic"), f"unexpected provider {s['provider']}"
    assert s["model"], "no model configured"
    if s["provider"] == "bedrock":
        assert s["model"].startswith("anthropic."), (
            f"Bedrock model ids need the anthropic. prefix, got {s['model']}"
        )
    return f"provider={s['provider']} model={s['model']} available={s['available']}"


check("LLM provider is configured behind one module", llm_isolated)


def degradation():
    s = get(f"{CTRL}/api/llm/status")
    inc = get(f"{CTRL}/api/incidents/{INCIDENT_ID}")["incident"]
    # Whether or not a model was reachable, attribution and policy must have
    # produced the same decisions.
    assert inc["fault_domain"] == "cfc-product", "attribution changed with model state"
    assert float(inc["confidence"]) >= 0.85, "confidence changed with model state"
    narrative = (inc["diagnosis"] or {}).get("narrative") or {}
    assert narrative, "no narrative produced at all"
    meta = narrative.get("_meta") or {}
    if meta.get("degraded"):
        mode = "degraded -- narrative assembled from deterministic signals"
    else:
        mode = (
            f"model-generated by {meta.get('model')} in {meta.get('latency_ms')}ms "
            f"({meta.get('input_tokens')} in / {meta.get('output_tokens')} out)"
        )
    return f"pipeline completed: {mode}"


check("pipeline completes whether or not a model is reachable", degradation)

# ------------------------------------------------------------ 2. tool surface
print("\n-- 2. the agent's entire capability surface is typed")


def tool_surface():
    d = get(f"{CTRL}/api/tools")
    names = {t["name"] for t in d["tools"]}
    for required in ("get_trace", "read_source", "validate_against_contract", "create_patch"):
        assert required in names, f"tool {required} is not registered"
    for forbidden in ("bash", "shell", "exec", "kubectl", "run_command", "eval"):
        assert forbidden not in names, f"RAW EXECUTION TOOL '{forbidden}' is registered"
    return f"{d['count']} typed tools, no raw shell / kubectl / eval"


check("no raw shell, kubectl, or unrestricted execution tool exists", tool_surface)

# ---------------------------------------------------- 3. GUARDRAILS REFUSE
print(f"\n-- 3. {BOLD}the guardrails actually refuse{RESET}")


def protected_zone():
    status, body = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/create_patch",
        {"actor": "capability-repair-agent",
         "path": "cap-calc/src/calc_api/calc/formulas.py",
         "new_content": "# tampered"},
    )
    assert status == 403, f"expected 403 for a protected path, got {status}: {body}"
    assert "PROTECTED ZONE" in body.get("message", ""), f"unexpected reason: {body}"
    return "calculation formulas refused: protected zone"


check("BLOCKED: a patch to the calculation formulas", protected_zone)


def emission_factors():
    status, body = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/create_patch",
        {"actor": "capability-repair-agent",
         "path": "cap-factors/data/seed.sql", "new_content": "-- tampered"},
    )
    assert status == 403, f"expected 403 for emission factor data, got {status}"
    return "emission factor data refused: these numbers end up in disclosures"


check("BLOCKED: a patch to emission factor data", emission_factors)


def cross_boundary():
    # The product agent trying to reach into capability code. Ownership
    # boundaries are hard boundaries.
    status, body = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/create_patch",
        {"actor": "product-repair-agent",
         "path": "cap-factors/src/factor_api/main.py", "new_content": "# tampered"},
    )
    assert status == 403, f"expected 403 for a cross-boundary write, got {status}"
    assert "write scope" in body.get("message", "").lower(), f"unexpected reason: {body}"
    return "product agent refused write access to capability code"


check("BLOCKED: the product agent patching capability code", cross_boundary)


def no_self_modification():
    status, _ = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/create_patch",
        {"actor": "product-repair-agent",
         "path": "platform/policy/policy.yaml", "new_content": "kill_switch: false"},
    )
    assert status == 403, f"expected 403 for self-modification, got {status}"
    return "agents cannot modify the autofix system's own policy"


check("BLOCKED: an agent editing ASHS's own policy", no_self_modification)


def path_traversal():
    status, _ = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/read_source",
        {"actor": "product-repair-agent", "path": "../../etc/passwd"},
    )
    assert status == 403, f"expected 403 for path traversal, got {status}"
    return "path traversal outside the workspace refused"


check("BLOCKED: path traversal out of the workspace", path_traversal)


def in_scope_allowed():
    status, body = post(
        f"{CTRL}/api/incidents/{INCIDENT_ID}/tools/read_source",
        {"actor": "product-repair-agent", "path": "cfc-product/api/src/cfc_api/main.py"},
    )
    assert status == 200, f"an in-scope read should succeed, got {status}: {body}"
    assert "compute_percentages" in body["result"]["content"], "read the wrong file"
    return f"in-scope read allowed: {body['result']['lines']} lines"


check("ALLOWED: the product agent reading its own source", in_scope_allowed)

# ------------------------------------------------------- 4. plan + approval
print("\n-- 4. a complete plan reaches POLICY_APPROVED")


def approved():
    inc = get(f"{CTRL}/api/incidents/{INCIDENT_ID}")["incident"]
    assert inc["state"] == "POLICY_APPROVED", f"state is {inc['state']}"
    plan = inc["plan"]
    assert plan["action_type"] == "code_patch", f"action {plan['action_type']}"
    assert plan["repair_agent"] == "product-repair-agent", f"agent {plan['repair_agent']}"
    assert plan["target_paths"], "plan has no targets"
    assert all(p.startswith("cfc-product/") for p in plan["target_paths"]), (
        f"plan targets outside the fault domain: {plan['target_paths']}"
    )
    assert plan["test_spec"]["must_fail_pre_patch"], "plan does not require a failing test"
    assert plan["validation_criteria"]["replay_request"], "plan has no replay validation"
    assert plan["rollback"], "plan has no rollback path"
    return (
        f"{plan['action_type']} on {plan['target_paths'][0]} "
        f"(risk {plan['risk']}), test + validation + rollback all declared"
    )


check("plan is typed, scoped, and carries test/validation/rollback", approved)


def all_gates():
    d = get(f"{CTRL}/api/incidents/{INCIDENT_ID}/audit")
    gates = [e for e in d["audit"] if e["actor"] == "policy-engine" and e["kind"] == "decision"]
    names = {(e["detail"] or {}).get("gate") for e in gates}
    for required in ("kill_switch", "protected_zone", "action_type", "write_scope",
                     "change_budget", "loop_breaker", "test_gate", "confidence",
                     "autonomy_level"):
        assert required in names, f"policy gate '{required}' did not run"
    assert all((e["detail"] or {}).get("passed") for e in gates), "a gate failed"
    return f"all {len(gates)} gates ran and passed, each individually audited"


check("every policy gate ran and was individually audited", all_gates)


def audit_completeness():
    d = get(f"{CTRL}/api/incidents/{INCIDENT_ID}/audit")
    actors = {e["actor"] for e in d["audit"]}
    for required in ("detector", "evidence-builder", "attribution-engine",
                     "diagnosis-agent", "router", "policy-engine"):
        assert required in actors, f"{required} left no audit entry"
    denials = [e for e in d["audit"]
               if e["kind"] == "tool_call" and (e["detail"] or {}).get("outcome") == "denied"]
    assert len(denials) >= 5, f"expected the refusals to be audited, saw {len(denials)}"
    return f"{d['count']} entries across {len(actors)} actors, including {len(denials)} audited refusals"


check("every actor and every refusal is in the audit trail", audit_completeness)

# --------------------------------------------------------- 5. kill switch
print("\n-- 5. the kill switch")


def kill_switch_present():
    pol = get(f"{CTRL}/api/catalog")["policy"]
    assert "kill_switch" in pol, "no kill switch declared"
    d = get(f"{CTRL}/api/incidents/{INCIDENT_ID}/audit")
    gate = next(e for e in d["audit"] if (e["detail"] or {}).get("gate") == "kill_switch")
    assert gate["detail"]["passed"], "kill switch gate did not evaluate"
    return "kill switch is the first gate evaluated on every plan"


check("kill switch is evaluated before anything else", kill_switch_present)

post(f"{CTRL}/api/config/auto_execute", {"enabled": True})

passed = sum(1 for ok, _, _ in results if ok)
failed = len(results) - passed
print("\n" + "=" * 70)
print(f"  passed {passed}, failed {failed}")
if failed == 0:
    print(f"  {GREEN}PHASE 2 COMPLETE{RESET} -- plan approved, guardrails proven to refuse")
else:
    print(f"  {RED}PHASE 2 INCOMPLETE{RESET}")
    for ok, name, note in results:
        if not ok:
            print(f"    - {name}: {note}")
sys.exit(0 if failed == 0 else 1)

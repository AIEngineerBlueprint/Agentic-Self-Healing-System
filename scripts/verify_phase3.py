#!/usr/bin/env python3
"""PHASE 3 EXIT TEST -- repair, verify, and revert.

Two runs, both required:

  A. Scenario 1 end to end to RESOLVED, with the test gate proven to have
     failed pre-patch and passed post-patch.
  B. A deliberately failed validation, proving the system ROLLS BACK to the
     known-good state and escalates rather than leaving a broken repair in
     place.

Run B matters at least as much as run A. A repair you cannot show reverting is
not a repair anyone should trust, and the byte-for-byte restore check below is
what makes "we rolled back" a fact rather than a claim.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFC = "http://localhost:8000"
CTRL = "http://localhost:8090"
SOURCE = pathlib.Path("services/cfc-product/api/src/cfc_api/main.py")
BASELINE = pathlib.Path(".baseline/cfc_api_main.py")

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results: list[tuple[bool, str, str]] = []
ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}


def get(url: str, timeout: float = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def post(url: str, payload: dict, timeout: float = 120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
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


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def reset_all() -> None:
    subprocess.run(["make", "reset-source"], capture_output=True, check=False)
    subprocess.run(["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs",
                    "-c", "DELETE FROM ash_incidents;"], capture_output=True, check=False)
    subprocess.run(["docker", "compose", "--env-file", ".env", "-f",
                    "services/cfc-product/docker-compose.yml", "restart", "cfc-api"],
                   capture_output=True, check=False)
    time.sleep(12)


def drive_incident() -> None:
    post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": True})
    for _ in range(8):
        post(f"{CFC}/api/footprint/calculate", ZERO)


TERMINAL = {"RESOLVED", "ESCALATED"}


def wait_terminal(timeout: float = 420.0) -> dict:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        incs = get(f"{CTRL}/api/incidents")["incidents"]
        active = [i for i in incs if i["state"] != "POLICY_APPROVED" or True]
        if active:
            last = active[-1]
            if last["state"] in TERMINAL:
                return last
        time.sleep(5)
    raise AssertionError(f"did not settle in {timeout:.0f}s; last={last['state'] if last else 'none'}")


def audit_of(incident_id: str) -> list[dict]:
    return get(f"{CTRL}/api/incidents/{incident_id}/audit")["audit"]


print("== Phase 3 exit test: repair, verify, revert " + "=" * 26)

# =========================================================== RUN A: RESOLVED
print(f"\n{BOLD}-- RUN A: autonomous repair to RESOLVED{RESET}")
post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": False})
reset_all()
BASE_SHA = sha(SOURCE)
drive_incident()
print(f"{DIM}   defect injected; waiting for the loop{RESET}")
A = wait_terminal()
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})
A_AUDIT = audit_of(A["incident_id"])
print(f"{DIM}   settled: {A['incident_id']} -> {A['state']}{RESET}")


def reached_resolved():
    assert A["state"] == "RESOLVED", f"ended at {A['state']}, expected RESOLVED"
    return f"{A['incident_id']} reached RESOLVED autonomously"


check("Scenario 1 completes autonomously to RESOLVED", reached_resolved)


def test_gate_enforced():
    pre = next((e for e in A_AUDIT if e["actor"] == "test-gate"
                and (e["detail"] or {}).get("phase") == "pre_patch"), None)
    post_ = next((e for e in A_AUDIT if e["actor"] == "test-gate"
                  and (e["detail"] or {}).get("phase") == "post_patch"), None)
    assert pre, "no pre-patch test run recorded"
    assert post_, "no post-patch test run recorded"
    assert pre["detail"]["passed"] is False, "the test PASSED before the patch -- it proves nothing"
    assert post_["detail"]["passed"] is True, "the test failed after the patch"
    return "test failed pre-patch and passed post-patch, both recorded"


check(f"{BOLD}TEST GATE: test failed before the fix and passed after{RESET}", test_gate_enforced)


def patch_is_real():
    assert sha(SOURCE) != BASE_SHA, "source is unchanged -- no patch was actually applied"
    text = SOURCE.read_text()
    assert "def compute_percentages" in text, "the patch destroyed the target function"
    # The fix must make correct behaviour correct, not hide the symptom.
    assert "except ZeroDivisionError" not in text.split("def compute_percentages")[1][:800], (
        "the patch swallowed the exception instead of fixing the behaviour"
    )
    compiled = subprocess.run([sys.executable, "-m", "py_compile", str(SOURCE)],
                              capture_output=True)
    assert compiled.returncode == 0, f"patched file does not compile: {compiled.stderr.decode()[:200]}"
    return f"source changed ({BASE_SHA} -> {sha(SOURCE)}), compiles, function intact"


check("a real code change was applied and it compiles", patch_is_real)


def validation_ran():
    checks = [e for e in A_AUDIT if e["actor"] == "validation-agent" and e["kind"] == "decision"]
    names = {(e["detail"] or {}).get("check") for e in checks}
    for required in ("replay_original_failing_request", "no_regression_on_normal_input",
                     "error_rate_returned_to_baseline"):
        assert required in names, f"validation check '{required}' did not run"
    assert all((e["detail"] or {}).get("passed") for e in checks), "a validation check failed"
    return f"all {len(checks)} validation checks passed, including the original replay"


check("validation replayed the original failing request", validation_ran)


def journey_healthy():
    status, body = post(f"{CFC}/api/footprint/calculate", ZERO)
    assert status == 200, f"zero-state journey still returns {status}"
    assert body["total_kgco2e"] == 0 and body["breakdown"] == [], f"wrong shape: {body}"
    status, body = post(f"{CFC}/api/footprint/calculate", {
        "period": "monthly", "region": "IN-KA",
        "activities": {"electricity_kwh": 320, "petrol_car_km": 800, "diet": "medium_meat"}})
    assert status == 200 and body["total_kgco2e"] > 0, "normal calculation regressed"
    return "zero state renders correctly AND normal calculations still work"


check("the user journey is genuinely healthy afterwards", journey_healthy)


def metrics_recorded():
    m = get(f"{CTRL}/api/incidents/{A['incident_id']}/metrics")["metrics"]
    names = {x["metric"] for x in m}
    assert "ttr_seconds" in names, "time-to-repair was not recorded"
    ttr = next(float(x["value"]) for x in m if x["metric"] == "ttr_seconds")
    return f"time to repair {ttr:.0f}s recorded from first_seen to RESOLVED"


check("repair metrics are recorded", metrics_recorded)

# ========================================================== RUN B: ROLLBACK
print(f"\n{BOLD}-- RUN B: forced validation failure must roll back{RESET}")
reset_all()
CLEAN_SHA = sha(SOURCE)
post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": True})
drive_incident()
print(f"{DIM}   validation forced to fail; waiting for the loop{RESET}")
B = wait_terminal()
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})
post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": False})
B_AUDIT = audit_of(B["incident_id"])
print(f"{DIM}   settled: {B['incident_id']} -> {B['state']}{RESET}")


def rolled_back():
    states = [e["summary"] for e in B_AUDIT if e["kind"] == "state_transition"]
    assert any("ROLLBACK" in s for s in states), f"no ROLLBACK transition; saw {states[-3:]}"
    assert B["state"] == "ESCALATED", f"ended at {B['state']}, expected ESCALATED"
    return "failed validation -> ROLLBACK -> ESCALATED"


check(f"{BOLD}a failed repair rolls back and escalates{RESET}", rolled_back)


def source_restored():
    assert sha(SOURCE) == CLEAN_SHA, (
        f"source was NOT restored: {sha(SOURCE)} != {CLEAN_SHA}. "
        f"A rollback that does not restore is not a rollback."
    )
    return f"source byte-identical to the pre-repair state ({CLEAN_SHA})"


check("rollback restored the source byte-for-byte", source_restored)


def service_healthy_after_rollback():
    status, body = post(f"{CFC}/api/footprint/calculate", {
        "period": "monthly", "region": "IN-KA",
        "activities": {"electricity_kwh": 320, "diet": "medium_meat"}})
    assert status == 200 and body["total_kgco2e"] > 0, f"service unhealthy after rollback: {status}"
    return "service serving normal traffic correctly after the revert"


check("the service is healthy after rollback", service_healthy_after_rollback)


def rollback_audited():
    tool_calls = [e for e in B_AUDIT if e["kind"] == "tool_call"
                  and (e["detail"] or {}).get("tool") == "restore_snapshot"]
    assert tool_calls, "the rollback itself was not audited"
    snap = [e for e in B_AUDIT if (e["detail"] or {}).get("tool") == "snapshot"]
    assert snap, "no snapshot was recorded before mutation"
    return "snapshot before mutation and restore after failure, both audited"


check("snapshot and restore are both in the audit trail", rollback_audited)

# --------------------------------------------------------------- final reset
reset_all()

passed = sum(1 for ok, _, _ in results if ok)
failed = len(results) - passed
print("\n" + "=" * 70)
print(f"  passed {passed}, failed {failed}")
if failed == 0:
    print(f"  {GREEN}PHASE 3 COMPLETE{RESET} -- repairs land, and failed repairs revert")
else:
    print(f"  {RED}PHASE 3 INCOMPLETE{RESET}")
    for ok, name, note in results:
        if not ok:
            print(f"    - {name}: {note}")
sys.exit(0 if failed == 0 else 1)

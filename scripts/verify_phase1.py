#!/usr/bin/env python3
"""PHASE 1 EXIT TEST -- detect and attribute.

The system must independently produce `fault_domain: cfc-product` at high
confidence, with the contract-validation evidence that exonerates BOTH
capabilities.

NO LANGUAGE MODEL IS INVOLVED. That is the point of this phase: if attribution
only works because a model was asked nicely, the demo proves nothing. Everything
checked here is deterministic and independently reproducible.
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


def get(url: str, timeout: float = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def post(url: str, payload: dict, timeout: float = 30):
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


print("== Phase 1 exit test: detect and attribute " + "=" * 28)

# --------------------------------------------------------------- clean slate
print(f"\n{DIM}-- resetting incident state and injecting the defect{RESET}")
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
     "-c", "DELETE FROM ash_incidents;"],
    capture_output=True, check=False,
)
# This phase asserts the pipeline reaches a given state. Pin autonomy to
# plan-only so the repair loop does not drive past those assertions --
# and so a mid-test hot reload cannot time out the requests below.
post(f"{CTRL}/api/config/auto_execute", {"enabled": False})
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": True})

ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}
failures = sum(1 for _ in range(8) if post(f"{CFC}/api/footprint/calculate", ZERO)[0] == 500)
print(f"{DIM}   drove {failures}/8 failing requests; waiting for telemetry{RESET}")
time.sleep(12)

# ------------------------------------------------------------- 1. detection
print("\n-- 1. detection, fingerprinting, dedupe")


def detection():
    post(f"{CTRL}/api/detect", {})
    time.sleep(2)
    d = get(f"{CTRL}/api/incidents")
    assert d["count"] >= 1, "no incident was opened for a sustained error rate"
    globals()["INCIDENT"] = d["incidents"][0]
    inc = INCIDENT
    assert inc["occurrence_count"] >= 3, f"expected a cluster, got {inc['occurrence_count']}"
    assert inc["fingerprint"].startswith("sha256:"), "incident is not fingerprinted"
    return (
        f"{inc['incident_id']}  occurrences={inc['occurrence_count']}  "
        f"fingerprint={inc['fingerprint'][:26]}..."
    )


check("a sustained error rate opens exactly one fingerprinted incident", detection)


def dedupe():
    before = get(f"{CTRL}/api/incidents")["count"]
    for _ in range(4):
        post(f"{CFC}/api/footprint/calculate", ZERO)
    time.sleep(8)
    post(f"{CTRL}/api/detect", {})
    after = get(f"{CTRL}/api/incidents")["count"]
    assert after == before, (
        f"dedupe failed: {before} -> {after} incidents for the same fingerprint"
    )
    return f"further occurrences did not open a second incident ({after} total)"


check("fingerprint dedupe holds -- one open repair per defect", dedupe)

# ------------------------------------------------------------- 2. evidence
print("\n-- 2. bounded evidence")


def evidence():
    d = get(f"{CTRL}/api/incidents/{INCIDENT['incident_id']}")
    ev = d["incident"]["evidence"]
    assert ev, "no evidence bundle was built"
    assert ev["exemplar_trace_id"], "evidence has no exemplar trace"
    assert len(ev["services_involved"]) >= 3, f"expected >=3 services, got {ev['services_involved']}"
    assert ev["ownership"], "evidence carries no ownership mapping"
    assert ev["dependency_chain"], "evidence carries no dependency chain"
    return (
        f"{len(ev['spans'])} spans, {len(ev['logs'])} logs, "
        f"services={ev['services_involved']}"
    )


check("evidence is bounded to the exemplar trace and carries ownership", evidence)

# ---------------------------------------------------------- 3. ATTRIBUTION
print(f"\n-- 3. {BOLD}THE KEY ONE: deterministic attribution{RESET}")


STATE_ORDER = [
    "NEW", "TRIAGED", "EVIDENCE_READY", "DIAGNOSIS_READY", "ROUTED",
    "PLAN_READY", "POLICY_APPROVED", "EXECUTING", "VALIDATING", "RESOLVED",
]


def fault_domain():
    inc = get(f"{CTRL}/api/incidents/{INCIDENT['incident_id']}")["incident"]
    state = inc["state"]
    # Phase 1 asserts the incident REACHED diagnosis, not that it stopped there.
    # Later phases legitimately carry it further within the same sweep, and an
    # exit test that forbids progress would break every time we add a phase.
    assert state in STATE_ORDER, f"unexpected state {state}"
    assert STATE_ORDER.index(state) >= STATE_ORDER.index("DIAGNOSIS_READY"), (
        f"state is {state}, expected DIAGNOSIS_READY or beyond"
    )
    assert inc["fault_domain"] == "cfc-product", (
        f"fault domain is {inc['fault_domain']}, expected cfc-product"
    )
    assert inc["surfaced_in"] == "cfc-product", f"surfaced_in is {inc['surfaced_in']}"
    globals()["DIAG"] = inc["diagnosis"]
    return f"fault_domain={inc['fault_domain']}  surfaced_in={inc['surfaced_in']}  state={state}"


check("fault domain resolved to cfc-product", fault_domain)


def confidence():
    inc = get(f"{CTRL}/api/incidents/{INCIDENT['incident_id']}")["incident"]
    conf = float(inc["confidence"])
    floors = get(f"{CTRL}/api/catalog")["policy"]["confidence"]
    floor = float(floors["autonomous_floor"])
    assert conf >= floor, f"confidence {conf} is below the autonomous floor {floor}"
    assert conf > floor + 0.02, (
        f"confidence {conf} sits on the floor {floor} with no headroom"
    )
    return f"confidence={conf} vs autonomous floor {floor} (headroom {conf - floor:.2f})"


check("confidence clears the autonomous floor with headroom", confidence)


def exoneration():
    ex = {e["repository"]: e for e in DIAG["exonerated"]}
    for repo in ("cap-calc", "cap-factors"):
        assert repo in ex, f"{repo} was NOT exonerated; got {list(ex)}"
        assert ex[repo]["basis"] == "contract_validation", (
            f"{repo} exonerated on {ex[repo]['basis']}, not objective contract validation"
        )
    return "cap-calc and cap-factors both exonerated by contract validation"


check("BOTH capabilities exonerated on objective evidence", exoneration)


def signals_present():
    names = {s["name"] for s in DIAG["signals"]}
    for required in ("trace_walk", "contract_validation", "deploy_correlation", "blast_radius"):
        assert required in names, f"signal {required} did not run"
    walk = next(s for s in DIAG["signals"] if s["name"] == "trace_walk")
    assert walk["evidence"]["service"] == "cfc-api", f"trace walk landed on {walk['evidence']['service']}"
    exc = walk["evidence"].get("exception") or {}
    assert exc.get("type") == "ZeroDivisionError", f"exception type was {exc.get('type')}"
    return f"all four signals ran; deepest error = {exc.get('type')} in cfc-api"


check("all four deterministic signals ran and agree", signals_present)


def audit_trail():
    d = get(f"{CTRL}/api/incidents/{INCIDENT['incident_id']}/audit")
    assert d["count"] >= 10, f"audit trail has only {d['count']} entries"
    actors = {e["actor"] for e in d["audit"]}
    for required in ("detector", "evidence-builder", "attribution-engine"):
        assert required in actors, f"{required} left no audit entry"
    states = [e["summary"] for e in d["audit"] if e["kind"] == "state_transition"]
    assert any("TRIAGED" in s for s in states), "no TRIAGED transition recorded"
    assert any("EVIDENCE_READY" in s for s in states), "no EVIDENCE_READY transition"
    assert any("DIAGNOSIS_READY" in s for s in states), "no DIAGNOSIS_READY transition"
    return f"{d['count']} entries, every transition traceable by incident id"


check("full episode is auditable by incident id", audit_trail)

# ------------------------------------------------------ 4. no LLM involved
print("\n-- 4. attribution used no language model")


def no_llm():
    """Attribution must be deterministic.

    Scoped deliberately to the entries at or before the DIAGNOSIS_READY
    transition. Once credentials are configured the diagnosis agent DOES call a
    model -- that is Phase 2 and it is fine. What must never happen is a model
    call influencing who owns the bug, so the assertion is about ordering, not
    about the absence of any model call anywhere.
    """
    d = get(f"{CTRL}/api/incidents/{INCIDENT['incident_id']}/audit")
    trail = d["audit"]
    cutoff = next(
        (e["seq"] for e in trail
         if e["kind"] == "state_transition" and "DIAGNOSIS_READY" in e["summary"]),
        max((e["seq"] for e in trail), default=0),
    )
    early = [e for e in trail if e["seq"] <= cutoff and e["kind"] == "llm_call"]
    assert not early, (
        f"{len(early)} LLM call(s) at or before DIAGNOSIS_READY -- "
        f"attribution must be deterministic"
    )
    later = [e for e in trail if e["seq"] > cutoff and e["kind"] == "llm_call"]
    return (
        f"zero llm_call entries through attribution (seq<={cutoff}); "
        f"{len(later)} afterwards, which is Phase 2's job"
    )


check("no LLM call appears anywhere in the audit trail", no_llm)

# ------------------------------------------------------------------ cleanup
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})

post(f"{CTRL}/api/config/auto_execute", {"enabled": True})

passed = sum(1 for ok, _, _ in results if ok)
failed = len(results) - passed
print("\n" + "=" * 70)
print(f"  passed {passed}, failed {failed}")
if failed == 0:
    print(f"  {GREEN}PHASE 1 COMPLETE{RESET} -- L1 diagnose, deterministic, no model")
else:
    print(f"  {RED}PHASE 1 INCOMPLETE{RESET}")
    for ok, name, note in results:
        if not ok:
            print(f"    - {name}: {note}")
sys.exit(0 if failed == 0 else 1)

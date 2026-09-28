#!/usr/bin/env python3
"""SCENARIO 1 -- the Product owns the bug, and the system fixes it.

The full loop, narrated for a projector:

    baseline -> inject -> detect -> attribute -> exonerate -> plan -> gate
             -> patch -> TEST GATE -> deploy -> validate -> RESOLVED

Run this beside the dashboard at http://localhost:3001. The dashboard shows the
decisions as they happen; this script narrates the argument.

    make demo-scenario-1              full autonomous repair
    make demo-scenario-1 ROLLBACK=1   forced validation failure -> revert
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFC, CALC, CTRL = "http://localhost:8000", "http://localhost:8081", "http://localhost:8090"
ROLLBACK_MODE = os.environ.get("ROLLBACK") == "1"

B, D, G, R, Y, C, M, X = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[35m", "\033[0m"
)
ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}


def post(url, payload, timeout=60):
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
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def step(n, title):
    print(f"\n{B}{C}[{n}]{X} {B}{title}{X}")


def rule():
    print(f"{B}{'=' * 76}{X}")


# --------------------------------------------------------------------- setup
rule()
print(f"{B}  SCENARIO 1 — the Product owns the bug{X}")
print(f"{D}  A legal, correct downstream response the product cannot handle.{X}")
if ROLLBACK_MODE:
    print(f"{Y}  ROLLBACK MODE: validation will be forced to fail.{X}")
print(f"{D}  Dashboard: http://localhost:3001{X}")
rule()

print(f"\n{D}Resetting to a clean baseline…{X}")
subprocess.run(["make", "reset-source"], capture_output=True, check=False)
subprocess.run(["docker", "compose", "--env-file", ".env", "-f",
                "services/cfc-product/docker-compose.yml", "restart", "cfc-api"],
               capture_output=True, check=False)
subprocess.run(["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs",
                "-c", "DELETE FROM ash_incidents;"], capture_output=True, check=False)
post(f"{CTRL}/api/config/auto_execute", {"enabled": True})
post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": ROLLBACK_MODE})
time.sleep(12)

# ------------------------------------------------------------------ baseline
step(1, "Baseline — an all-zero calculation is a legitimate zero state")
status, body = post(f"{CFC}/api/footprint/calculate", ZERO)
print(f"    {G}HTTP {status}{X}   total={body.get('total_kgco2e')}   breakdown={body.get('breakdown')}")
print(f"    {D}A footprint of zero is a correct answer, not an error.{X}")

# -------------------------------------------------------------------- inject
step(2, "Inject the defect — a runtime flag, no restart, no redeploy")
post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": True})
print(f"    {Y}zero_total_division enabled on cfc-api{X}")
time.sleep(1)

# ------------------------------------------------- the capability is correct
step(3, "The capability is still CORRECT — calc-api, called directly")
status, calc = post(f"{CALC}/v1/calculate", ZERO)
print(f"    {G}HTTP {status}{X}   total={calc.get('total_kgco2e')}   breakdown={calc.get('breakdown')}")
print(f"    {D}Validates cleanly against calc-api's own published schema.{X}")
print(f"    {D}An empty breakdown for all-zero input is contract-compliant BY DESIGN.{X}")

# ------------------------------------------------------------- product fails
step(4, "The same request through the PRODUCT now fails")
failures = 0
for _ in range(8):
    st, _b = post(f"{CFC}/api/footprint/calculate", ZERO)
    failures += st >= 500
print(f"    {R}HTTP 500 × {failures}{X}   the user sees a generic failure")
print(f"    {D}It tells them nothing about the cause.{X}")

# ------------------------------------------------------- watch the loop work
step(5, "The system takes over — watch the dashboard")
print(f"    {D}detect → evidence → attribute → route → plan → policy → "
      f"patch → test gate → validate{X}\n")

TERMINAL = {"RESOLVED", "ESCALATED"}
seen_state, seen_seq, incident_id = None, 0, None
deadline = time.time() + 420

while time.time() < deadline:
    try:
        incidents = get(f"{CTRL}/api/incidents")["incidents"]
    except Exception:  # noqa: BLE001
        time.sleep(3); continue
    if not incidents:
        time.sleep(3); continue

    inc = incidents[0]
    incident_id = inc["incident_id"]

    trail = get(f"{CTRL}/api/incidents/{incident_id}/audit")["audit"]
    for e in trail:
        if e["seq"] <= seen_seq:
            continue
        seen_seq = e["seq"]
        s, actor, det = e["summary"], e["actor"], (e["detail"] or {})

        if e["kind"] == "state_transition":
            colour = G if ("RESOLVED" in s or "POLICY_APPROVED" in s) else (
                R if ("ESCALATED" in s or "ROLLBACK" in s) else C)
            print(f"    {colour}{s[:96]}{X}")
        elif "EXONERATED" in s:
            print(f"      {G}{s[:94]}{X}")
        elif e["kind"] == "llm_call":
            print(f"      {M}{actor}: {s[:80]}{X}")
            if det.get("model"):
                print(f"        {D}{det['model']} · {det.get('latency_ms')}ms · "
                      f"{det.get('input_tokens')}→{det.get('output_tokens')} tok{X}")
        elif actor == "test-gate":
            ok = det.get("passed")
            phase = det.get("phase")
            want = "must FAIL" if phase == "pre_patch" else "must PASS"
            good = (phase == "pre_patch" and not ok) or (phase == "post_patch" and ok)
            print(f"      {G if good else R}TEST GATE {phase}: "
                  f"{'passed' if ok else 'failed'}  ({want}){X}")
        elif actor == "validation-agent" and det.get("check"):
            print(f"      {G if det.get('passed') else R}[{det['check']}] "
                  f"{'PASS' if det.get('passed') else 'FAIL'}{X}")

    if inc["state"] != seen_state:
        seen_state = inc["state"]
        if seen_state in TERMINAL:
            break
    time.sleep(2)

post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})
post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": False})

# -------------------------------------------------------------------- result
step(6, "Outcome")
inc = get(f"{CTRL}/api/incidents/{incident_id}")["incident"]
metrics = {m["metric"]: float(m["value"])
           for m in get(f"{CTRL}/api/incidents/{incident_id}/metrics")["metrics"]}

print(f"    incident      {inc['incident_id']}")
print(f"    state         {(G if inc['state'] == 'RESOLVED' else R)}{inc['state']}{X}")
print(f"    fault domain  {B}{inc['fault_domain']}{X}   (surfaced in {inc['surfaced_in']})")
print(f"    confidence    {inc['confidence']}")
if metrics.get("ttr_seconds"):
    print(f"    time to repair {metrics['ttr_seconds']:.0f}s")

diag = inc.get("diagnosis") or {}
if diag.get("exonerated"):
    print(f"\n    {B}Exonerated on objective evidence{X}")
    for e in diag["exonerated"]:
        print(f"      {G}{e['repository']:<13}{X} {e['reason'][:70]}")

if inc["state"] == "RESOLVED":
    st, body = post(f"{CFC}/api/footprint/calculate", ZERO)
    print(f"\n    {B}User journey now{X}: {G}HTTP {st}{X} "
          f"total={body.get('total_kgco2e')} breakdown={body.get('breakdown')}")
    print(f"\n{D}    The capability was never touched, and the system can show you{X}")
    print(f"{D}    exactly why it was ruled out.{X}")
else:
    print(f"\n{D}    The repair was reverted and escalated. Nothing was left broken.{X}")

rule()
sys.exit(0 if inc["state"] in TERMINAL else 1)

#!/usr/bin/env python3
"""Three consecutive clean runs from a full reset.

The Phase 4 acceptance criterion. A demo you can only run once is not a demo --
it is a recording. Each cycle does a REAL `make reset` (volumes destroyed, data
reseeded, source restored), then drives Scenario 1 to RESOLVED, and every cycle
must reproduce the same outcome.
"""
from __future__ import annotations
import json, subprocess, sys, time, urllib.error, urllib.request

CFC, CTRL = "http://localhost:8000", "http://localhost:8090"
G, R, D, B, X = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}
runs = []


def post(url, payload, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        # An HTTP error IS the response. Collapsing it into a bare except and
        # returning 0 made real 500s read as "0/8 requests failed" -- an
        # acceptance test that misreports its own inputs is worse than none.
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {}
    except Exception:  # noqa: BLE001 - transport failure, genuinely not a response
        return 0, {}


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            get(f"{CTRL}/health"); get(f"{CFC}/api/health")
            return time.time()
        except Exception:  # noqa: BLE001
            time.sleep(3)
    raise AssertionError("stack did not come up")


for cycle in range(1, 4):
    print(f"\n{B}{'=' * 68}{X}")
    print(f"{B}  CYCLE {cycle} of 3 — full reset, then Scenario 1{X}")
    print(f"{B}{'=' * 68}{X}")

    t0 = time.time()
    print(f"{D}  make reset (volumes destroyed, data reseeded, source restored)…{X}")
    subprocess.run(["make", "reset"], capture_output=True, check=False)
    ready = wait_ready()
    boot = ready - t0

    # Verify the reset actually took. A silent no-op here looks exactly like a
    # flaky demo later -- the loop breaker counts historical failures, so a DB
    # that survives reset will block the third run every time.
    left = subprocess.run(
        ["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs",
         "-tAc", "SELECT count(*) FROM ash_incidents;"],
        capture_output=True, text=True).stdout.strip()
    print(f"{D}  stack healthy in {boot:.0f}s · incidents after reset: {left}{X}")
    if left not in ("0", ""):
        print(f"  {R}reset did not clear the incident store ({left} rows){X}")

    # Let the collector and traffic generator establish a baseline.
    time.sleep(20)
    post(f"{CTRL}/api/config/auto_execute", {"enabled": True})
    post(f"{CTRL}/api/_demo/force_validation_failure", {"enabled": False})

    t1 = time.time()
    post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": True})
    failures = sum(1 for _ in range(8) if post(f"{CFC}/api/footprint/calculate", ZERO)[0] >= 500)
    print(f"{D}  defect injected, {failures}/8 requests failed{X}")

    state, incident, deadline = None, None, time.time() + 480
    while time.time() < deadline:
        try:
            incs = get(f"{CTRL}/api/incidents")["incidents"]
        except Exception:  # noqa: BLE001
            time.sleep(4); continue
        if incs:
            incident = incs[0]
            state = incident["state"]
            if state in ("RESOLVED", "ESCALATED"):
                break
        time.sleep(5)

    elapsed = time.time() - t1
    post(f"{CFC}/_demo/bug", {"name": "zero_total_division", "enabled": False})

    total_incidents = subprocess.run(
        ["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs",
         "-tAc", "SELECT count(*), count(*) FILTER (WHERE state IN ('ROLLBACK','ESCALATED')) "
                 "FROM ash_incidents;"],
        capture_output=True, text=True).stdout.strip()
    print(f"{D}  incidents this cycle: {total_incidents} (total|failed){X}")

    # Independent proof the journey actually recovered.
    st, body = post(f"{CFC}/api/footprint/calculate", ZERO)
    healthy = st == 200 and body.get("total_kgco2e") == 0 and body.get("breakdown") == []

    # RESOLVED alone is no longer proof of a repair: an incident whose defect
    # stopped reproducing before the patch landed also closes as RESOLVED, with
    # outcome 'stale' and nothing shipped. A clean run must have patched.
    outcome = (incident or {}).get("outcome")
    ok = state == "RESOLVED" and outcome == "repaired" and healthy
    runs.append({
        "cycle": cycle,
        "state": f"{state}·stale" if outcome == "stale" else state,
        "ok": ok,
        "boot_s": round(boot), "loop_s": round(elapsed),
        "fault": incident.get("fault_domain") if incident else None,
        "confidence": float(incident["confidence"]) if incident and incident.get("confidence") else None,
        "journey_healthy": healthy,
    })
    print(f"  {G if ok else R}cycle {cycle}: {state} in {elapsed:.0f}s "
          f"(journey healthy: {healthy}){X}")

print(f"\n{B}{'=' * 68}{X}")
print(f"{B}  RESULTS{X}")
print(f"{B}{'=' * 68}{X}")
print(f"  {'cycle':<7}{'outcome':<12}{'fault':<14}{'conf':<7}{'boot':<8}{'loop':<8}journey")
for r in runs:
    mark = G if r["ok"] else R
    print(f"  {mark}{r['cycle']:<7}{str(r['state']):<12}{str(r['fault']):<14}"
          f"{str(r['confidence'] or '—'):<7}{str(r['boot_s']) + 's':<8}"
          f"{str(r['loop_s']) + 's':<8}{'healthy' if r['journey_healthy'] else 'DEGRADED'}{X}")

passed = sum(1 for r in runs if r["ok"])
print(f"\n  {passed}/3 clean runs")
if passed == 3:
    boots = [r["boot_s"] for r in runs]
    loops = [r["loop_s"] for r in runs]
    print(f"  {G}REPEATABLE{X} — cold start {min(boots)}-{max(boots)}s, "
          f"repair loop {min(loops)}-{max(loops)}s")
else:
    print(f"  {R}NOT REPEATABLE{X}")
sys.exit(0 if passed == 3 else 1)

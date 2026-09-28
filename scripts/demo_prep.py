#!/usr/bin/env python3
"""Ready the stack for a demo WITHOUT destroying the incident board.

A full `make reset` wipes the volumes, which takes every past incident with it.
That is right before a test run and wrong before a demo -- the board is the
evidence that this thing has been working, and it should survive until someone
clears it on purpose (`make clear-incidents`).

Three things have to be true before a demo:
  1. no bug flag left enabled anywhere,
  2. the service source restored to baseline (the Makefile does this),
  3. no outstanding escalation holding the re-detection cool-off open.

(3) is the subtle one. An escalated incident suppresses re-detection of its own
fingerprint for ten minutes so a freshly paged human is not buried. Acknowledge
it and the window has served its purpose -- which is exactly what a presenter
re-running the demo means by "I have seen it, carry on".
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

CTRL = "http://localhost:8090"
# Every service that exposes the demo fault-injection endpoint.
BUG_ENDPOINTS = {
    "cfc-api": "http://localhost:8000",
    "calc-api": "http://localhost:8081",
    "factor-api": "http://localhost:8082",
}


def request(url: str, payload=None, method="GET", timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


cleared = []
for service, base in BUG_ENDPOINTS.items():
    try:
        state = request(f"{base}/_demo/bug")
    except Exception:  # noqa: BLE001 - prep must never block the demo
        print(f"  ! {service} unreachable — skipped")
        continue
    for flag in state.get("enabled_flags") or []:
        try:
            request(f"{base}/_demo/bug", {"name": flag, "enabled": False}, "POST")
            cleared.append(f"{service}:{flag}")
        except Exception:  # noqa: BLE001
            pass

print(f"  bug flags cleared: {', '.join(cleared) if cleared else 'none were set'}")

acked = 0
try:
    for escalation in request(f"{CTRL}/api/escalations").get("escalations", []):
        try:
            request(f"{CTRL}/api/escalations/{escalation['incident_id']}/ack",
                    payload={}, method="POST")
            acked += 1
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001 - prep must never block the demo
    print("  ! control plane unreachable — escalations not acknowledged")
    sys.exit(0)

print(f"  escalations acknowledged: {acked}  (re-detection cool-off cleared)")

# The blast-radius signal is worth 0.22 and only pays out when the suspect's
# DEPENDENCIES are quiet -- across a ten-minute lookback. A capability fault
# from a previous rehearsal is still inside that window, so act 1 scores 0.70
# (human review) instead of 0.92 (autonomous repair) and never reaches the test
# gate. That is the signal being right and the rehearsal cadence being wrong,
# but it is baffling live unless someone says so first.
try:
    import subprocess
    recent = subprocess.run(
        ["docker", "exec", "ashs-ashs-db-1", "psql", "-U", "ashs", "-d", "ashs", "-tAc",
         "SELECT COALESCE(EXTRACT(EPOCH FROM (interval '10 minutes' "
         "- (now() - MAX(start_time)))), -1) FROM tel_spans "
         "WHERE status_code = 'ERROR' AND service_name IN ('calc-api', 'factor-api')"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    wait = float(recent or -1)
    if wait > 0:
        print(f"\n  ! capability errors from a previous run are still inside the")
        print(f"    blast-radius window for another {wait / 60:.0f} min.")
        print(f"    Act 1 will score 0.70 (human review) instead of 0.92 "
              f"(autonomous repair).")
        print(f"    Wait it out, or run `make reset` for a pristine board.")
except Exception:  # noqa: BLE001 - advisory only, never block the demo
    pass

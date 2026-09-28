#!/usr/bin/env python3
"""THE FIVE-MINUTE DEMO. Two acts, because one is not the argument.

    ACT 1  the PRODUCT owns it                       (~85s)
           surfaced_in == fault_domain == cfc-product, both capabilities
           actively exonerated. 0.92 clears the autonomous floor, so it
           repairs itself through the test gate.

    ACT 2  the CAPABILITY owns it, two hops down     (~40s)
           surfaced_in cfc-product, fault_domain cap-factors. The two DIFFER,
           which is the whole claim -- and because factor-api runs at
           autonomy L1, it drafts the patch and refuses to merge it.

Act 1 proves the repair is real. Act 2 proves the attribution is real: it
routed past the service where the error appeared. Showing only Act 1 leaves
"surfaced in" and "fault domain" reading identically, which looks like the
system simply blames wherever the error surfaced.

ORDER MATTERS. The blast-radius signal wants the dependencies quiet and looks
back ten minutes; running the capability act first leaves its errors inside
that window and costs the product act 0.22 of confidence -- 0.92 becomes 0.70,
and it asks for a human instead of repairing. Ending on the refusal is also
the better close.

    make demo             both acts
    ACT=1 make demo       the repair only
    ACT=2 make demo       the attribution only

The full narrated version is `make demo-scenario-1`.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

CFC, CALC, CTRL = "http://localhost:8000", "http://localhost:8081", "http://localhost:8090"

B, D, G, R, Y, C, M, X = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[35m", "\033[0m"
)
ZERO = {"period": "monthly", "region": "IN-KA", "activities": {}}

T0 = time.time()


def clock() -> str:
    """Elapsed wall time, so the presenter always knows where they are."""
    elapsed = int(time.time() - T0)
    return f"{D}{elapsed // 60}:{elapsed % 60:02d}{X}"


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


def beat(n: int, title: str) -> None:
    print(f"\n{clock()}  {B}{n}. {title}{X}")


ACT = os.environ.get("ACT", "")
FACTOR = "http://localhost:8082"
LOADED = {"period": "monthly", "region": "IN-KA",
          "activities": {"electricity_kwh": 320, "petrol_car_km": 800}}


MIN_OCCURRENCES = 3   # detection threshold in policy.yaml


def flag(base: str, name: str, enabled: bool, confirm: bool = True) -> bool:
    """Set a demo bug flag and confirm the service agrees it is set.

    These services run under `uvicorn --reload` and reset their flags on
    startup, so a source restore landing a moment earlier can silently clear
    the flag between setting it and using it. Confirming turns that from a
    baffling half-broken demo into a fact we can act on.
    """
    post(f"{base}/_demo/bug", {"name": name, "enabled": enabled})
    if not confirm:
        return True
    for _ in range(10):
        try:
            state = get(f"{base}/_demo/bug")
            if (name in (state.get("enabled_flags") or [])) == enabled:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def drive(payload: dict, base: str, name: str, n: int = 8) -> int:
    """Send n requests, return how many failed -- the demo's proof the defect
    is live. Retries once if a mid-flight reload cleared the flag."""
    for attempt in (1, 2):
        failures = sum(1 for _ in range(n)
                       if post(f"{CFC}/api/footprint/calculate", payload)[0] >= 500)
        if failures >= MIN_OCCURRENCES or attempt == 2:
            return failures
        # Not enough to trip detection: the service almost certainly reloaded
        # and dropped the flag. Re-arm and drive once more.
        time.sleep(2)
        flag(base, name, True)
    return 0


def settle(budget: float = 120) -> None:
    """Wait until nothing is mid-flight. One injected fault can open several
    incidents (blast radius); the next act must not inherit them."""
    deadline = time.time() + budget
    while time.time() < deadline:
        try:
            live = [i for i in get(f"{CTRL}/api/incidents")["incidents"]
                    if i["state"] not in ("RESOLVED", "ESCALATED")]
        except Exception:  # noqa: BLE001
            time.sleep(2); continue
        if not live:
            return
        time.sleep(3)


def board_now() -> set[str]:
    try:
        return {i["incident_id"] for i in get(f"{CTRL}/api/incidents")["incidents"]}
    except Exception:  # noqa: BLE001
        return set()


def follow(pre_existing: set[str], budget: float = 150,
           prefer_surfaced_in: str | None = None) -> dict | None:
    """Narrate the incident THIS run caused. Never one already on the board.

    An earlier version followed incidents[0], which on a re-run replayed a
    previous episode's audit trail at full speed and exited 'successful' in
    zero seconds -- indistinguishable, on a projector, from a fake.
    """
    seen_seq, incident, deadline = 0, None, time.time() + budget
    while time.time() < deadline:
        try:
            fresh = [i for i in get(f"{CTRL}/api/incidents")["incidents"]
                     if i["incident_id"] not in pre_existing]
        except Exception:  # noqa: BLE001
            time.sleep(2); continue
        if not fresh:
            time.sleep(2); continue

        # One fault can surface in more than one service. Narrate the one the
        # user would actually see -- the product entry point -- not whichever
        # happens to be newest.
        incident = next((i for i in fresh if i.get("surfaced_in") == prefer_surfaced_in),
                        fresh[0]) if prefer_surfaced_in else fresh[0]
        for e in get(f"{CTRL}/api/incidents/{incident['incident_id']}/audit")["audit"]:
            if e["seq"] <= seen_seq:
                continue
            seen_seq = e["seq"]
            summary, actor, detail = e["summary"], e["actor"], (e["detail"] or {})

            if "EXONERATED" in summary:
                print(f"   {G}{summary[:92]}{X}")
            elif "contract_validation" in summary and "does not" in summary.lower():
                print(f"   {R}{summary[:92]}{X}")
            elif actor == "test-gate" and detail.get("phase"):
                phase, passed = detail["phase"], detail.get("passed")
                want = "must FAIL" if phase == "pre_patch" else "must PASS"
                good = (phase == "pre_patch") != bool(passed)
                print(f"   {G if good else R}TEST GATE {phase}: "
                      f"{'passed' if passed else 'failed'}  ({want}){X}")
            elif e["kind"] == "state_transition":
                for key in ("DIAGNOSIS_READY", "POLICY_APPROVED", "RESOLVED",
                            "ROLLBACK", "ESCALATED"):
                    if key in summary:
                        colour = R if key in ("ROLLBACK", "ESCALATED") else (
                            G if key == "RESOLVED" else C)
                        print(f"   {colour}{summary[:92]}{X}")
                        break

        if incident["state"] in ("RESOLVED", "ESCALATED"):
            return get(f"{CTRL}/api/incidents/{incident['incident_id']}")["incident"]
        time.sleep(2)
    return None


def verdict(inc: dict) -> None:
    """The two lines the whole demo exists to put on screen."""
    routed = inc["surfaced_in"] != inc["fault_domain"]
    print(f"\n   surfaced in   {inc['surfaced_in']}   {D}where it broke{X}")
    print(f"   fault domain  {B}{inc['fault_domain']}{X}   "
          f"{(Y + 'ROUTED PAST the service that failed' + X) if routed else (D + 'same owner' + X)}")
    print(f"   confidence    {inc['confidence']}")
    for e in (inc.get("diagnosis") or {}).get("exonerated") or []:
        print(f"   {G}exonerated    {e['repository']}{X}  {D}{e['reason'][:58]}{X}")


print(f"\n{B}ASHS — five minute demo{X}   {D}dashboard: http://localhost:3001{X}")

try:
    board = {i["incident_id"] for i in get(f"{CTRL}/api/incidents")["incidents"]}
except Exception:  # noqa: BLE001
    print(f"   {R}control plane unreachable at {CTRL}{X}")
    sys.exit(1)

failed_acts = []

# ============================================================ ACT 1  (product — repairs)
if ACT in ("", "1"):
    beat(1, "The product owns it — and this one it repairs")
    status, body = post(f"{CFC}/api/footprint/calculate", ZERO)
    print(f"   {G}zero state  HTTP {status}{X}  total={body.get('total_kgco2e')}  "
          f"{D}a legitimate answer{X}")

    flag(CFC, "zero_total_division", True)
    status, calc = post(f"{CALC}/v1/calculate", ZERO)
    print(f"   {G}calc-api    HTTP {status}{X}  total={calc.get('total_kgco2e')}  "
          f"{D}still contract-compliant{X}")

    failures = drive(ZERO, CFC, "zero_total_division")
    print(f"   {R}product     HTTP 500 × {failures}{X}  {D}same request, "
          f"through the product{X}")

    if failures < MIN_OCCURRENCES:
        print(f"\n   {R}Only {failures} failure(s) — detection needs "
              f"{MIN_OCCURRENCES}.{X}")
        print(f"   {D}Either cfc-api is still carrying a patch from an earlier "
              f"run, or it reloaded and dropped the flag.{X}")
        print(f"\n   {B}Run: make demo-prep{X}  {D}(restores source, keeps history){X}\n")
        flag(CFC, "zero_total_division", False)
        sys.exit(1)

    print()
    inc = follow(board)
    flag(CFC, "zero_total_division", False)

    if inc is None:
        print(f"\n   {R}No incident opened.{X} {D}Check `make incidents`.{X}\n")
        sys.exit(1)

    verdict(inc)
    metrics = {m["metric"]: float(m["value"])
               for m in get(f"{CTRL}/api/incidents/{inc['incident_id']}/metrics")["metrics"]}
    if metrics.get("ttr_seconds"):
        print(f"   time to repair {metrics['ttr_seconds']:.0f}s")

    status, body = post(f"{CFC}/api/footprint/calculate", ZERO)
    print(f"\n   {B}user journey{X}  {G if status == 200 else R}HTTP {status}{X}  "
          f"total={body.get('total_kgco2e')}  breakdown={body.get('breakdown')}")
    if not (inc["state"] == "RESOLVED" and inc.get("outcome") == "repaired"):
        failed_acts.append("1")

    settle()
    board = board_now()   # everything act 1 caused is now history

# ============================================================ ACT 2  (capability — refuses)
if ACT in ("", "2"):
    beat(2, "A different fault — and this one it refuses to touch")
    flag(FACTOR, "null_factor_new_region", True)
    failures = drive(LOADED, FACTOR, "null_factor_new_region")
    print(f"   {R}product  HTTP 500 × {failures}{X}   "
          f"{D}the user sees a product failure{X}")

    if failures < MIN_OCCURRENCES:
        print(f"\n   {R}Only {failures} failure(s) — detection needs "
              f"{MIN_OCCURRENCES}.{X}")
        print(f"   {D}factor-api likely reloaded and dropped the flag. "
              f"Wait a few seconds and re-run.{X}\n")
        flag(FACTOR, "null_factor_new_region", False)
        sys.exit(1)

    print(f"   {D}watch the dashboard — where does it say the fault lives?{X}\n")
    inc = follow(board, prefer_surfaced_in="cfc-product")
    flag(FACTOR, "null_factor_new_region", False)

    if inc is None:
        print(f"\n   {R}No incident opened.{X} {D}Check `make incidents`.{X}\n")
        sys.exit(1)

    verdict(inc)
    print(f"\n   {D}It walked the trace, validated each capability's payload "
          f"against its own{X}")
    print(f"   {D}published schema, and landed two repositories away from where "
          f"the error appeared.{X}")
    others = len(board_now() - board) - 1
    if others > 0:
        print(f"   {D}({others} further incident(s) opened from the same fault — "
              f"the middle capability failed too, and was attributed to the same "
              f"owner.){X}")

    failed_acts += [] if inc["state"] in ("RESOLVED", "ESCALATED") else ["2"]

print(f"\n{clock()}  {D}every decision above is in the audit trail, "
      f"by incident id{X}\n")
sys.exit(1 if failed_acts else 0)

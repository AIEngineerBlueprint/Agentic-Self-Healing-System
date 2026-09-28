#!/usr/bin/env python3
"""PHASE 0 EXIT TEST.

One calculation from the product must produce ONE trace spanning cfc-api,
calc-api and factor-api, visible in ashs-db. If that fails, context propagation
is broken and every later phase is built on sand.

Written in Python rather than inline shell so the assertions are readable and
their failures are legible -- an exit test whose own quoting is fragile is worse
than no exit test.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

FACT = "http://localhost:8082"
CALC = "http://localhost:8081"
CFC = "http://localhost:8000"
CTRL = "http://localhost:8090"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

results: list[tuple[bool, str, str]] = []


def get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def post(url: str, payload: dict, timeout: float = 25) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")}


def check(name: str, fn) -> None:
    try:
        note = fn() or ""
        results.append((True, name, note))
        print(f"  {GREEN}PASS{RESET}  {name}" + (f"\n        {DIM}{note}{RESET}" if note else ""))
    except Exception as exc:  # noqa: BLE001 - the exit test reports, never raises
        results.append((False, name, str(exc)))
        print(f"  {RED}FAIL{RESET}  {name}\n        {RED}{exc}{RESET}")


REAL_INPUT = {
    "period": "monthly",
    "region": "IN-KA",
    "activities": {
        "electricity_kwh": 320,
        "petrol_car_km": 800,
        "rail_km": 120,
        "flights_short_haul": 1,
        "diet": "medium_meat",
        "waste_kg": 40,
        "recycling_percent": 30,
    },
}

print("== Phase 0 exit test " + "=" * 50)

# ---------------------------------------------------------------- 1. health
print("\n-- 1. services healthy")
for label, url in (
    ("factor-api", f"{FACT}/health"),
    ("calc-api", f"{CALC}/health"),
    ("cfc-api", f"{CFC}/api/health"),
    ("ashs-control", f"{CTRL}/health"),
):
    check(label, lambda u=url: get(u).get("status", "?"))

# ------------------------------------------------------------- 2. contracts
print("\n-- 2. contracts published, generated from code")


def contract(url: str, must_have: str):
    doc = get(url)
    assert doc.get("paths"), "no paths in OpenAPI document"
    assert must_have in doc["paths"], f"{must_have} missing from published contract"
    return f"{len(doc['paths'])} paths"


check("factor-api /openapi.json", lambda: contract(f"{FACT}/openapi.json", "/v1/factors/bulk"))
check("calc-api /openapi.json", lambda: contract(f"{CALC}/openapi.json", "/v1/calculate"))
check("cfc-api /openapi.json", lambda: contract(f"{CFC}/openapi.json", "/api/footprint/calculate"))

# ----------------------------------------------------------- 3. calculation
print("\n-- 3. a real calculation is coherent")


def real_calculation():
    status, d = post(f"{CFC}/api/footprint/calculate", REAL_INPUT)
    assert status == 200, f"expected 200, got {status}: {d}"
    assert d["total_kgco2e"] > 0, "total should be positive"
    assert len(d["breakdown"]) >= 5, f"expected >=5 categories, got {len(d['breakdown'])}"
    assert d["factor_dataset_version"], "dataset version must be present on every response"

    share_sum = sum(e["share"] for e in d["breakdown"])
    assert abs(share_sum - 1.0) < 0.02, f"shares must sum to 1.0, got {share_sum:.4f}"

    pct_sum = sum(e["percent"] for e in d["breakdown"])
    assert abs(pct_sum - 100.0) < 2.0, f"percentages must sum to 100, got {pct_sum:.1f}"

    # Arithmetic spot-check against the seeded IN-KA grid factor of 0.71.
    elec = next(e for e in d["breakdown"] if e["category"] == "electricity")
    assert abs(elec["kgco2e"] - 320 * 0.71) < 0.5, f"electricity should be 227.2, got {elec['kgco2e']}"

    return (
        f"total={d['total_kgco2e']} kg  annualised={d['annualised_tco2e']} t  "
        f"categories={len(d['breakdown'])}  dataset={d['factor_dataset_version']}"
    )


check("calculation returns a coherent, arithmetically correct result", real_calculation)

# ------------------------------------------------------------ 4. zero state
print("\n-- 4. zero input is a correct zero state, not an error")


def zero_state():
    status, d = post(f"{CFC}/api/footprint/calculate", {**REAL_INPUT, "activities": {}})
    assert status == 200, f"zero input must be HTTP 200, got {status}"
    assert d["total_kgco2e"] == 0, f"expected total 0, got {d['total_kgco2e']}"
    assert d["breakdown"] == [], f"expected empty breakdown, got {d['breakdown']}"
    return "HTTP 200, total 0.0, empty breakdown -- contract-compliant"


check("all-zero input handled correctly by the product", zero_state)


def capability_zero_state():
    status, d = post(f"{CALC}/v1/calculate", {**REAL_INPUT, "activities": {}})
    assert status == 200, f"calc-api must return 200 for zero input, got {status}"
    assert d["total_kgco2e"] == 0.0 and d["breakdown"] == []
    return "calc-api independently returns total 0.0 + empty breakdown"


check("capability's zero response is itself correct", capability_zero_state)

# -------------------------------------------------------------- 5. contract
print("\n-- 5. factor contract rules hold")


def typed_404():
    try:
        get(f"{FACT}/v1/factors?region=ZZ-NOWHERE&activity=electricity_grid")
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"expected 404, got {e.code}"
        body = json.loads(e.read())
        assert body.get("error") == "region_not_found", f"expected typed error, got {body}"
        return "unknown region -> 404 with typed body, no silent default"
    raise AssertionError("unknown region did not 404 -- a default may be leaking")


check("unknown region returns a typed 404", typed_404)

# ------------------------------------------------------------- 6. telemetry
print("\n-- 6. telemetry landed in ashs-db")
time.sleep(6)


def telemetry_ingested():
    d = get(f"{CTRL}/api/stats")
    assert d["spans"] > 0, "no spans ingested -- check the collector exporter"
    assert d["logs"] > 0, "no logs ingested"
    assert d["services"] >= 3, f"expected >=3 services, saw {d['services']}"
    return f"spans={d['spans']} traces={d['traces']} logs={d['logs']} services={d['services']}"


check("spans and logs ingested from at least 3 services", telemetry_ingested)

# ------------------------------------------------------- 7. THE KEY ONE
print("\n-- 7. THE KEY ONE: a single trace crosses the ownership boundary")


def cross_boundary_trace():
    d = get(f"{CTRL}/api/traces/recent/full")
    assert d["count"] > 0, "no multi-service traces -- trace context is not propagating"

    t = d["traces"][0]
    services = set(t["services"])
    for required in ("cfc-api", "calc-api", "factor-api"):
        assert required in services, f"{required} missing from trace; saw {sorted(services)}"

    detail = get(f"{CTRL}/api/traces/{t['trace_id']}")
    repos = {s["code_repository"] for s in detail["spans"] if s["code_repository"]}
    assert repos >= {"cfc-product", "cap-calc", "cap-factors"}, (
        f"code.repository missing on some spans; saw {sorted(repos)}"
    )

    return (
        f"trace {t['trace_id'][:16]}... {t['span_count']} spans across "
        f"{sorted(services)}\n        repositories resolved: {sorted(repos)}"
    )


check("one calculation -> one trace across all three services", cross_boundary_trace)

# ------------------------------------------------------------------ report
passed = sum(1 for ok, _, _ in results if ok)
failed = len(results) - passed

print("\n" + "=" * 70)
print(f"  passed {passed}, failed {failed}")
if failed == 0:
    print(f"  {GREEN}PHASE 0 COMPLETE{RESET} -- harness verified, ready for Phase 1")
else:
    print(f"  {RED}PHASE 0 INCOMPLETE{RESET}")
    for ok, name, note in results:
        if not ok:
            print(f"    - {name}: {note}")
sys.exit(0 if failed == 0 else 1)

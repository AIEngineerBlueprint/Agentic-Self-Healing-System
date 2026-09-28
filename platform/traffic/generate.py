"""Synthetic baseline traffic.

Produces a low, steady rate of SUCCESSFUL calculations so the error-rate graph
has something to spike against. A flat zero line is not convincing -- an error
rate that climbs off a real baseline is.

Inputs are randomised across regions, periods, and plausible household profiles
so the traces are varied rather than a single repeated request.
"""

import os
import random
import time

import httpx

CFC_API_URL = os.environ.get("CFC_API_URL", "http://cfc-api:8080")
RATE = float(os.environ.get("RATE_PER_SECOND", "1.0"))
REGIONS = ["IN-KA", "IN-MH", "GB", "US-CA", "DE", "FR"]
DIETS = ["high_meat", "medium_meat", "low_meat", "pescatarian", "vegetarian", "vegan"]


def profile() -> dict:
    """A plausible household. Never all-zero -- that is the demo operator's move."""
    return {
        "electricity_kwh": round(random.uniform(120, 650), 1),
        "natural_gas_m3": round(random.uniform(0, 40), 1),
        "lpg_kg": round(random.choice([0, 0, random.uniform(5, 20)]), 1),
        "heating_oil_litres": 0,
        "petrol_car_km": round(random.uniform(0, 1400), 0),
        "diesel_car_km": round(random.choice([0, 0, random.uniform(100, 900)]), 0),
        "electric_car_km": round(random.choice([0, 0, random.uniform(100, 800)]), 0),
        "bus_km": round(random.uniform(0, 200), 0),
        "two_wheeler_km": round(random.choice([0, random.uniform(50, 500)]), 0),
        "rail_km": round(random.uniform(0, 400), 0),
        "flights_short_haul": random.choice([0, 0, 0, 1, 2]),
        "flights_medium_haul": random.choice([0, 0, 0, 1]),
        "flights_long_haul": random.choice([0, 0, 0, 0, 1]),
        "diet": random.choice(DIETS),
        "waste_kg": round(random.uniform(10, 70), 1),
        "recycling_percent": round(random.uniform(0, 85), 0),
    }


def main() -> None:
    interval = 1.0 / RATE if RATE > 0 else 1.0
    sent = ok = failed = 0
    print(f"traffic-gen -> {CFC_API_URL} at {RATE}/s", flush=True)

    with httpx.Client(timeout=15.0) as client:
        while True:
            payload = {
                "period": random.choice(["monthly", "monthly", "annual"]),
                "region": random.choice(REGIONS),
                "activities": profile(),
            }
            try:
                r = client.post(f"{CFC_API_URL}/api/footprint/calculate", json=payload)
                sent += 1
                if r.status_code == 200:
                    ok += 1
                else:
                    failed += 1
            except httpx.RequestError:
                sent += 1
                failed += 1

            if sent % 30 == 0:
                print(f"sent={sent} ok={ok} failed={failed}", flush=True)

            time.sleep(interval * random.uniform(0.7, 1.3))


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as exc:  # noqa: BLE001 - the generator must never die
            print(f"traffic-gen restarting after {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)

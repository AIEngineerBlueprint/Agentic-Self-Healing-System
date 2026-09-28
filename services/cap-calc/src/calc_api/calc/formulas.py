"""Footprint calculation. The domain core of the demo.

PROTECTED ZONE. This module is listed in platform/policy/policy.yaml and may
never be autonomously modified by a repair agent. The reason is the same one
that protects the factor tables: these coefficients and this arithmetic decide
what a user is told their carbon output is.

--- Unit and period conventions, stated once, applied everywhere -------------

A calculation covers a `period`, either "monthly" (1 month) or "annual"
(12 months). Inputs fall into three classes, and getting these wrong is how
carbon calculators end up off by 12x:

  1. PER-PERIOD inputs -- electricity, heating, all travel. The user enters the
     amount consumed during the chosen period. Used as entered.

  2. INHERENTLY ANNUAL inputs -- diet. Published dietary factors are kgCO2e per
     person per YEAR (Scarborough et al. give a daily figure; cap-factors stores
     it annualised). Scaled by period_months / 12.

  3. PER-MONTH inputs -- waste. The UI asks for kg per month because that is how
     people actually know it. Scaled by period_months.

--- Boundary ----------------------------------------------------------------

Scope 1 and 2 for household energy, plus selected Scope 3 (travel, diet, waste).
Excludes: embodied emissions of goods and buildings, water, public services,
capital. This is a simplified boundary and the result is indicative only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Period name -> number of months it spans.
PERIOD_MONTHS = {"monthly": 1, "annual": 12}

# 1.5C-aligned per-capita target, tCO2e/person/year by 2030.
# Source: UNEP Emissions Gap Report / IGES 1.5-Degree Lifestyles.
TARGET_ANNUAL_TCO2E = 2.3

DIET_CHOICES = (
    "high_meat",
    "medium_meat",
    "low_meat",
    "pescatarian",
    "vegetarian",
    "vegan",
)

# Which factor keys each category consumes. calc-api uses this to build a single
# bulk lookup against factor-api, rather than N round trips.
CATEGORY_FACTORS: dict[str, tuple[str, ...]] = {
    "electricity": ("electricity_grid",),
    "heating": ("natural_gas", "lpg", "heating_oil"),
    "road_travel": ("petrol_car", "diesel_car", "electric_car", "bus", "two_wheeler"),
    "rail_travel": ("rail",),
    "air_travel": ("flight_short_haul", "flight_medium_haul", "flight_long_haul"),
    "diet": tuple(f"diet_{d}" for d in DIET_CHOICES),
    "waste": ("waste_landfill", "waste_recycled"),
}


def required_activities(activities: dict) -> list[str]:
    """The minimal set of factor keys needed for this input. Keeps the bulk
    lookup honest instead of fetching the whole table every time."""
    needed: set[str] = set()
    for category, keys in CATEGORY_FACTORS.items():
        if category == "diet":
            choice = activities.get("diet")
            if choice in DIET_CHOICES:
                needed.add(f"diet_{choice}")
            continue
        needed.update(keys)
    return sorted(needed)


@dataclass
class CategoryResult:
    category: str
    kgco2e: float
    # Line-item detail, so the product can explain *why* a number is what it is.
    components: dict[str, float] = field(default_factory=dict)


def _f(factors: dict[str, float], key: str) -> float:
    """Look up a factor.

    No defensive default. A missing or non-numeric factor must raise here rather
    than silently becoming zero -- a calculator that quietly treats a missing
    coefficient as "no emissions" is worse than one that fails loudly, and the
    ASHS attribution engine needs the failure to be visible.
    """
    value = factors[key]
    if value is None:
        raise TypeError(f"factor '{key}' is null; cap-factors contract requires a number")
    if isinstance(value, str):
        raise TypeError(f"factor '{key}' is a string; cap-factors contract requires a number")
    return float(value)


def _num(activities: dict, key: str) -> float:
    """Read a non-negative numeric activity input, defaulting to zero."""
    raw = activities.get(key, 0) or 0
    value = float(raw)
    if value < 0:
        raise ValueError(f"activity '{key}' must be >= 0, got {value}")
    return value


def compute_categories(
    activities: dict, factors: dict[str, float], period: str
) -> list[CategoryResult]:
    """Compute kgCO2e per category for the given period.

    Returns only categories with a non-zero result. An all-zero input therefore
    yields an EMPTY list -- which is legal, correct, and exactly what the
    Scenario 1 defect fails to handle downstream.
    """
    months = PERIOD_MONTHS[period]
    results: list[CategoryResult] = []

    # --- Electricity: kWh x grid intensity --------------------------------
    kwh = _num(activities, "electricity_kwh")
    if kwh:
        value = kwh * _f(factors, "electricity_grid")
        results.append(
            CategoryResult("electricity", value, {"grid_electricity": value})
        )

    # --- Heating: three fuels, each in its own unit ------------------------
    heating: dict[str, float] = {}
    for input_key, factor_key, label in (
        ("natural_gas_m3", "natural_gas", "natural_gas"),
        ("lpg_kg", "lpg", "lpg"),
        ("heating_oil_litres", "heating_oil", "heating_oil"),
    ):
        amount = _num(activities, input_key)
        if amount:
            heating[label] = amount * _f(factors, factor_key)
    if heating:
        results.append(CategoryResult("heating", sum(heating.values()), heating))

    # --- Road travel: km x per-km factor -----------------------------------
    road: dict[str, float] = {}
    for input_key, factor_key in (
        ("petrol_car_km", "petrol_car"),
        ("diesel_car_km", "diesel_car"),
        ("electric_car_km", "electric_car"),
        ("bus_km", "bus"),
        ("two_wheeler_km", "two_wheeler"),
    ):
        km = _num(activities, input_key)
        if km:
            road[factor_key] = km * _f(factors, factor_key)
    if road:
        results.append(CategoryResult("road_travel", sum(road.values()), road))

    # --- Rail: passenger-km ------------------------------------------------
    rail_km = _num(activities, "rail_km")
    if rail_km:
        value = rail_km * _f(factors, "rail")
        results.append(CategoryResult("rail_travel", value, {"rail": value}))

    # --- Air: count of ONE-WAY flights x per-flight factor -----------------
    air: dict[str, float] = {}
    for input_key, factor_key in (
        ("flights_short_haul", "flight_short_haul"),
        ("flights_medium_haul", "flight_medium_haul"),
        ("flights_long_haul", "flight_long_haul"),
    ):
        count = _num(activities, input_key)
        if count:
            air[factor_key] = count * _f(factors, factor_key)
    if air:
        results.append(CategoryResult("air_travel", sum(air.values()), air))

    # --- Diet: annual factor, apportioned to the period --------------------
    diet_choice = activities.get("diet")
    if diet_choice:
        if diet_choice not in DIET_CHOICES:
            raise ValueError(
                f"diet must be one of {', '.join(DIET_CHOICES)}, got '{diet_choice}'"
            )
        value = _f(factors, f"diet_{diet_choice}") * (months / 12.0)
        results.append(CategoryResult("diet", value, {diet_choice: value}))

    # --- Waste: kg/month x months, blended by recycling rate ---------------
    waste_kg_month = _num(activities, "waste_kg")
    if waste_kg_month:
        recycled_pct = _num(activities, "recycling_percent")
        if recycled_pct > 100:
            raise ValueError(f"recycling_percent must be 0-100, got {recycled_pct}")
        recycled = recycled_pct / 100.0
        blended = recycled * _f(factors, "waste_recycled") + (1 - recycled) * _f(
            factors, "waste_landfill"
        )
        value = waste_kg_month * months * blended
        results.append(
            CategoryResult(
                "waste",
                value,
                {"landfilled": value * (1 - recycled), "recycled": value * recycled},
            )
        )

    return results


def build_breakdown(categories: list[CategoryResult], total: float) -> list[dict]:
    """Breakdown entries with each category's share of the total.

    calc-api can compute share safely because it only ever iterates categories
    that exist -- an empty list means zero iterations, so there is no division.
    The consumer that blindly divides by a zero total is where Scenario 1 breaks.
    """
    if not categories:
        return []
    return [
        {
            "category": c.category,
            "kgco2e": round(c.kgco2e, 2),
            "share": round(c.kgco2e / total, 4) if total > 0 else 0.0,
            "components": {k: round(v, 2) for k, v in c.components.items()},
        }
        for c in sorted(categories, key=lambda c: c.kgco2e, reverse=True)
    ]


def annualise(total_kgco2e: float, period: str) -> float:
    """Scale a period total to tonnes CO2e per year."""
    return (total_kgco2e * (12 / PERIOD_MONTHS[period])) / 1000.0

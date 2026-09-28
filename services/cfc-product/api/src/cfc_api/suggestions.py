"""Reduction suggestions, computed from the user's actual breakdown.

Not a hardcoded list. Each suggestion is generated only when the relevant
category is actually material for this user, and the estimated saving is derived
from their own numbers. A calculator that tells a vegan to eat less meat is the
kind of detail that costs you the room.

Savings percentages are deliberately conservative and each carries its basis.
"""

from __future__ import annotations

# (category, headline, detail, fraction of that category's emissions saved, basis)
_RULES = [
    (
        "electricity",
        "Switch to a certified renewable electricity tariff",
        "Removes grid intensity from your largest controllable line item. Where a "
        "green tariff is unavailable, rooftop solar achieves a similar reduction "
        "over its lifetime.",
        0.60,
        "Assumes a supplier-backed renewable tariff displacing grid average intensity.",
    ),
    (
        "air_travel",
        "Replace one flight with rail, or hold one trip",
        "Aviation is the highest-intensity activity per hour of travel in this "
        "model, and includes a 1.9x radiative forcing uplift.",
        0.35,
        "Assumes roughly one third of flights are substitutable or deferrable.",
    ),
    (
        "road_travel",
        "Shift 30% of car distance to rail, bus, or cycling",
        "Rail is 5-8x lower per passenger-km than a private car in every region "
        "in this dataset.",
        0.28,
        "Assumes 30% modal shift from car to rail at that region's rail factor.",
    ),
    (
        "diet",
        "Move one meat-based meal per day to plant-based",
        "The gap between a high-meat and a low-meat diet is roughly 900 kgCO2e "
        "per person per year in this dataset.",
        0.25,
        "Scarborough et al. 2014, step change between adjacent dietary bands.",
    ),
    (
        "heating",
        "Improve insulation, or replace the boiler with a heat pump",
        "A well-specified heat pump delivers 3-4 units of heat per unit of "
        "electricity, which beats direct combustion in most grids.",
        0.40,
        "Assumes fabric-first improvements plus a COP-3 heat pump.",
    ),
    (
        "waste",
        "Raise recycling to 75% and separate food waste",
        "Landfilled organic waste generates methane; separated and recycled "
        "material is close to net zero in this model.",
        0.55,
        "Assumes recycling rate raised to 75% with food waste diverted.",
    ),
    (
        "rail_travel",
        "Rail is already your lowest-intensity travel mode",
        "No reduction suggested. Keeping journeys on rail is what is holding this "
        "category down.",
        0.0,
        "Informational only.",
    ),
]

# Below this, a category is noise and a suggestion would be busywork.
_MIN_MATERIAL_KG = 1.0


def build_suggestions(breakdown: list[dict], period: str, activities: dict) -> list[dict]:
    """Return up to three suggestions, ranked by estimated saving.

    An empty breakdown yields an empty list -- there is nothing to suggest
    reducing when there is nothing to reduce. That is a correct zero state, and
    the UI renders it as such.
    """
    if not breakdown:
        return []

    by_category = {entry["category"]: entry["kgco2e"] for entry in breakdown}
    scale = 12 if period == "monthly" else 1
    diet_choice = activities.get("diet")

    candidates: list[dict] = []
    for category, headline, detail, fraction, basis in _RULES:
        kg = by_category.get(category, 0.0)
        if kg < _MIN_MATERIAL_KG or fraction <= 0:
            continue

        # Do not tell someone already at the floor to go further.
        if category == "diet" and diet_choice in ("vegan", "vegetarian"):
            continue
        if category == "waste" and activities.get("recycling_percent", 0) >= 75:
            continue
        if category == "electricity" and activities.get("electricity_kwh", 0) <= 0:
            continue

        saving_period = kg * fraction
        candidates.append(
            {
                "category": category,
                "headline": headline,
                "detail": detail,
                "estimated_saving_kgco2e": round(saving_period, 1),
                "estimated_annual_saving_kgco2e": round(saving_period * scale, 1),
                "basis": basis,
            }
        )

    candidates.sort(key=lambda c: c["estimated_annual_saving_kgco2e"], reverse=True)
    return candidates[:3]

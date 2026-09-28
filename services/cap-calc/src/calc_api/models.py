"""Published contract for cap-calc.

These models ARE the contract. `/openapi.json` is generated from them and the
ASHS attribution engine validates real payloads against that document.

Contract rules (from the requirements, enforced below):
  - `total_kgco2e` is a required number. Never null, never a string.
  - `breakdown` MAY BE EMPTY when all activity inputs are zero. This is legal,
    and Scenario 1 depends on it being legal.
  - `share` is a number in [0, 1] inclusive.
  - `factor_dataset_version` is present on every response.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Period = Literal["monthly", "annual"]
Diet = Literal["high_meat", "medium_meat", "low_meat", "pescatarian", "vegetarian", "vegan"]


class Activities(BaseModel):
    """Activity data for one period. Every field optional; omitted means zero.

    Units are explicit in the field names because unit confusion is the single
    most common source of wrong answers in carbon accounting.
    """

    electricity_kwh: float = Field(0, ge=0, description="kWh consumed in the period")

    natural_gas_m3: float = Field(0, ge=0, description="Cubic metres in the period")
    lpg_kg: float = Field(0, ge=0, description="Kilograms in the period")
    heating_oil_litres: float = Field(0, ge=0, description="Litres in the period")

    petrol_car_km: float = Field(0, ge=0)
    diesel_car_km: float = Field(0, ge=0)
    electric_car_km: float = Field(0, ge=0)
    bus_km: float = Field(0, ge=0)
    two_wheeler_km: float = Field(0, ge=0)
    rail_km: float = Field(0, ge=0)

    flights_short_haul: float = Field(0, ge=0, description="One-way flights, <1500km")
    flights_medium_haul: float = Field(0, ge=0, description="One-way flights, 1500-4000km")
    flights_long_haul: float = Field(0, ge=0, description="One-way flights, >4000km")

    diet: Diet | None = Field(None, description="Dietary pattern; annual factor apportioned")

    waste_kg: float = Field(0, ge=0, description="Kilograms per MONTH (not per period)")
    recycling_percent: float = Field(0, ge=0, le=100)


class CalculateRequest(BaseModel):
    period: Period = "monthly"
    region: str = Field(..., description="Region code, e.g. IN-KA")
    activities: Activities

    model_config = {
        "json_schema_extra": {
            "example": {
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
        }
    }


class BreakdownEntry(BaseModel):
    category: str
    kgco2e: float = Field(..., description="Emissions for this category, kgCO2e")
    share: float = Field(..., ge=0, le=1, description="Share of total, 0..1 inclusive")
    components: dict[str, float] = Field(
        default_factory=dict, description="Line-item detail behind the category total"
    )


class CalculateResponse(BaseModel):
    calculation_id: str
    total_kgco2e: float = Field(
        ..., description="Required number. Never null, never a string."
    )
    period: Period
    breakdown: list[BreakdownEntry] = Field(
        default_factory=list,
        description=(
            "MAY BE EMPTY when all activity inputs are zero. An empty breakdown "
            "with total_kgco2e 0.0 is a correct, contract-compliant response."
        ),
    )
    annualised_tco2e: float
    factor_dataset_version: str
    computed_at: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "calculation_id": "calc_01J8XQ4M2K",
                "total_kgco2e": 759.5,
                "period": "monthly",
                "breakdown": [
                    {
                        "category": "electricity",
                        "kgco2e": 227.2,
                        "share": 0.2991,
                        "components": {"grid_electricity": 227.2},
                    }
                ],
                "annualised_tco2e": 9.11,
                "factor_dataset_version": "2026.1",
                "computed_at": "2026-08-12T09:14:02Z",
            }
        }
    }


class TypedError(BaseModel):
    error: str
    message: str
    detail: dict | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    dependencies: dict[str, str]


class BugFlagState(BaseModel):
    enabled_flags: list[str]
    available_flags: dict[str, str]

"""Published contract for cfc-product's backend for frontend."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Period = Literal["monthly", "annual"]
Diet = Literal["high_meat", "medium_meat", "low_meat", "pescatarian", "vegetarian", "vegan"]


class Activities(BaseModel):
    electricity_kwh: float = Field(0, ge=0)
    natural_gas_m3: float = Field(0, ge=0)
    lpg_kg: float = Field(0, ge=0)
    heating_oil_litres: float = Field(0, ge=0)
    petrol_car_km: float = Field(0, ge=0)
    diesel_car_km: float = Field(0, ge=0)
    electric_car_km: float = Field(0, ge=0)
    bus_km: float = Field(0, ge=0)
    two_wheeler_km: float = Field(0, ge=0)
    rail_km: float = Field(0, ge=0)
    flights_short_haul: float = Field(0, ge=0)
    flights_medium_haul: float = Field(0, ge=0)
    flights_long_haul: float = Field(0, ge=0)
    diet: Diet | None = None
    waste_kg: float = Field(0, ge=0, description="Kilograms per month")
    recycling_percent: float = Field(0, ge=0, le=100)


class CalculateRequest(BaseModel):
    period: Period = "monthly"
    region: str
    activities: Activities


class BreakdownEntry(BaseModel):
    category: str
    kgco2e: float
    share: float = Field(..., ge=0, le=1)
    percent: float = Field(..., description="Display percentage, 0..100")
    components: dict[str, float] = Field(default_factory=dict)


class Comparison(BaseModel):
    regional_average_tco2e: float | None = None
    regional_average_source: str | None = None
    target_tco2e: float
    target_label: str
    vs_regional_average_pct: float | None = None
    vs_target_pct: float


class Suggestion(BaseModel):
    category: str
    headline: str
    detail: str
    estimated_saving_kgco2e: float
    estimated_annual_saving_kgco2e: float
    basis: str


class FootprintResponse(BaseModel):
    calculation_id: str
    period: Period
    region: str
    region_name: str
    total_kgco2e: float
    annualised_tco2e: float
    breakdown: list[BreakdownEntry] = Field(default_factory=list)
    comparison: Comparison
    suggestions: list[Suggestion] = Field(default_factory=list)
    factor_dataset_version: str = Field(
        ..., description="Shown on screen. The audience must see when factors change."
    )
    computed_at: str
    disclaimer: str


class Region(BaseModel):
    code: str
    name: str
    country: str
    avg_annual_tco2e: float
    avg_source: str


class RegionsResponse(BaseModel):
    regions: list[Region]
    dataset_version: str


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

"""Published contract for cap-factors.

These models ARE the contract. `/openapi.json` is generated from them, and the
ASHS attribution engine validates real response payloads against that document.
A hand-maintained spec would silently break the whole attribution model, so
nothing here is duplicated by hand anywhere.

Contract rules (from the requirements, enforced below):
  - `factor` is a required number >= 0. Never null, never a string.
  - An unknown region returns 404 with a typed error body, never a silent default.
  - `dataset_version` is present on every response.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Factor(BaseModel):
    """A single emission factor. `factor` is required and non-null by contract."""

    activity: str = Field(..., description="Activity key, e.g. electricity_grid")
    region: str = Field(..., description="Region code, e.g. IN-KA")
    year: int = Field(..., description="Dataset year")
    factor: float = Field(
        ...,
        ge=0,
        description="Emission factor. Required, numeric, >= 0. Never null.",
    )
    unit: str = Field(..., description="Unit, e.g. kgCO2e/kWh")
    source: str = Field(..., description="Published source for this value")
    dataset_version: str = Field(..., description="Required on every response")

    model_config = {
        "json_schema_extra": {
            "example": {
                "activity": "electricity_grid",
                "region": "IN-KA",
                "year": 2026,
                "factor": 0.71,
                "unit": "kgCO2e/kWh",
                "source": "CEA CO2 Baseline Database v20",
                "dataset_version": "2026.1",
            }
        }
    }


class BulkFactorResponse(BaseModel):
    """Batch lookup. This is what calc-api calls on every calculation."""

    region: str
    year: int
    factors: list[Factor]
    dataset_version: str


class Region(BaseModel):
    code: str
    name: str
    country: str
    avg_annual_tco2e: float = Field(
        ..., description="Territorial per-capita annual footprint, tCO2e/person/yr"
    )
    avg_source: str


class RegionListResponse(BaseModel):
    regions: list[Region]
    dataset_version: str


class TypedError(BaseModel):
    """Typed error body. An unknown region gets this, not a default factor."""

    error: str = Field(..., description="Machine-readable error code")
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

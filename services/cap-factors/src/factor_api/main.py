"""factor-api -- the cap-factors capability.

Source of truth for emission coefficients. Owned by the cap-factors team; the
product never calls it directly, only calc-api does.

Contract rules enforced here:
  - `factor` is a required number >= 0. Never null, never a string.
  - Unknown region -> 404 with a typed error body, never a silent default.
  - `dataset_version` on every response.

Those three rules are what make objective attribution possible: the ASHS
attribution engine validates the actual payload this service returned against
the schema this service published. Invalid payload means the fault is here.
Valid payload plus a broken consumer means the fault is the consumer.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse

from common import disclaimer
from common.bugflags import BugFlags
from common.jsonlog import setup_logging
from common.telemetry import instrument_app, setup_telemetry

from . import db
from .models import (
    BugFlagState,
    BulkFactorResponse,
    Factor,
    HealthResponse,
    Region,
    RegionListResponse,
    TypedError,
)

SERVICE_NAME = "factor-api"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")
DEPLOYMENT_STACK = "cap-factors"
CODE_REPOSITORY = "cap-factors"
DEFAULT_YEAR = int(os.environ.get("FACTOR_YEAR", "2026"))

setup_logging(SERVICE_NAME, SERVICE_VERSION)
tracer = setup_telemetry(SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_STACK, CODE_REPOSITORY)
log = logging.getLogger(__name__)

# Demo fault-injection flags. Names are documented in the repo README and
# readable at GET /_demo/bug.
BUGS = BugFlags(
    {
        "null_factor_new_region": (
            "Return factor: null for the IN-KA region, violating this service's "
            "own published schema. Scenario 2 -- capability owns the fault."
        ),
        "factor_as_string": (
            "Return factor as a string ('0.71') instead of a number. Silent "
            "corruption -- calc-api concatenates instead of adding, no error."
        ),
        "wrong_grid_factor": (
            "Return a plausible but incorrect electricity coefficient. No error, "
            "totals shift ~40%. Scenario 3 -- the system must refuse to act."
        ),
    }
)

app = FastAPI(
    title="cap-factors :: factor-api",
    version=SERVICE_VERSION,
    description=(
        "Emission factor lookup for the ASHS demo. Source of truth for emission "
        "coefficients." + disclaimer.OPENAPI_NOTE
    ),
)
instrument_app(app)


@app.on_event("startup")
def _startup() -> None:
    db.init_pool()
    BUGS.reset()  # flags never survive a restart
    log.info("factor-api ready, dataset_version=%s", db.dataset_version())


def _apply_bug_flags(row: dict) -> dict:
    """Apply demo fault injection to an outbound factor row.

    Every mutation here is a deliberate, named contract violation. The database
    itself is never corrupted -- these scenarios are API-layer only, so a reset
    is a flag toggle rather than a reseed.
    """
    out = dict(row)

    if BUGS.is_on("null_factor_new_region") and row["region_code"] == "IN-KA":
        # Violates: factor is a required number. This is the Scenario 2 defect.
        out["factor"] = None
    elif BUGS.is_on("factor_as_string"):
        # Violates: factor is a number, never a string. Silent corruption.
        out["factor"] = str(float(row["factor"]))
    elif BUGS.is_on("wrong_grid_factor") and row["activity"] == "electricity_grid":
        # Contract-VALID but factually wrong. No schema check catches this,
        # which is exactly the point of Scenario 3.
        out["factor"] = float(row["factor"]) * 1.62

    return out


def _to_factor_payload(row: dict) -> dict:
    r = _apply_bug_flags(row)
    factor = r["factor"]
    return {
        "activity": r["activity"],
        "region": r["region_code"],
        "year": r["year"],
        # float() would raise on an injected None, so pass it through untouched
        # and let the payload be genuinely schema-invalid. That realism is the
        # whole point -- contract validation must have something real to catch.
        "factor": factor if factor is None or isinstance(factor, str) else float(factor),
        "unit": r["unit"],
        "source": r["source"],
        "dataset_version": r["dataset_version"],
    }


def _region_not_found(region: str) -> JSONResponse:
    """Typed 404. Never a silent default -- that is a contract rule."""
    return JSONResponse(
        status_code=404,
        content=TypedError(
            error="region_not_found",
            message=f"No emission factors are published for region '{region}'.",
            detail={"region": region, "dataset_version": db.dataset_version()},
        ).model_dump(),
    )


@app.get("/v1/factors", response_model=Factor, responses={404: {"model": TypedError}})
def get_factor(
    region: str = Query(..., description="Region code, e.g. IN-KA"),
    activity: str = Query(..., description="Activity key, e.g. electricity_grid"),
    year: int = Query(DEFAULT_YEAR, description="Dataset year"),
):
    span = __import__("opentelemetry").trace.get_current_span()
    span.set_attribute("factor.region", region)
    span.set_attribute("factor.activity", activity)

    if not db.region_exists(region):
        return _region_not_found(region)

    row = db.get_factor(region, activity, year)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=TypedError(
                error="factor_not_found",
                message=f"No factor for activity '{activity}' in region '{region}'.",
                detail={"region": region, "activity": activity, "year": year},
            ).model_dump(),
        )

    return JSONResponse(content=_to_factor_payload(row))


@app.get(
    "/v1/factors/bulk",
    response_model=BulkFactorResponse,
    responses={404: {"model": TypedError}, 422: {"model": TypedError}},
)
def get_factors_bulk(
    region: str = Query(..., description="Region code"),
    activities: str = Query(..., description="Comma-separated activity keys"),
    year: int = Query(DEFAULT_YEAR),
):
    """Batch lookup. calc-api calls this once per calculation."""
    span = __import__("opentelemetry").trace.get_current_span()
    span.set_attribute("factor.region", region)

    if not db.region_exists(region):
        return _region_not_found(region)

    keys = [a.strip() for a in activities.split(",") if a.strip()]
    span.set_attribute("factor.activity_count", len(keys))

    rows = db.get_factors_bulk(region, keys, year)
    return JSONResponse(
        content={
            "region": region,
            "year": year,
            "factors": [_to_factor_payload(r) for r in rows],
            "dataset_version": db.dataset_version(),
        }
    )


@app.get("/v1/regions", response_model=RegionListResponse)
def list_regions():
    return RegionListResponse(
        regions=[Region(**r) for r in db.list_regions()],
        dataset_version=db.dataset_version(),
    )


@app.get("/health", response_model=HealthResponse)
def health(response: Response):
    db_status = db.ping()
    ok = db_status == "ok"
    response.status_code = 200 if ok else 503
    return HealthResponse(
        status="healthy" if ok else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        dependencies={"factor-db": db_status},
    )


@app.get("/_demo/bug", response_model=BugFlagState)
def get_bug_flags():
    return BugFlagState(**BUGS.state())


@app.post("/_demo/bug", response_model=BugFlagState)
def set_bug_flag(body: dict):
    if not BUGS.demo_enabled:
        return JSONResponse(
            status_code=403,
            content={"error": "demo_disabled", "message": "DEMO_BUGS_ENABLED is not true."},
        )
    try:
        BUGS.set(body["name"], bool(body.get("enabled", False)))
    except KeyError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "unknown_flag", "message": f"Unknown flag: {exc}"},
        )
    return BugFlagState(**BUGS.state())

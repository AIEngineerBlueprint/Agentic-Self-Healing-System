"""calc-api -- the cap-calc capability.

Synchronous footprint calculation. Owned by the cap-calc team. Consumes
cap-factors over a published contract; consumed by cfc-api over its own.

The Scenario 1 point of this service: given all-zero activity input it returns
`total_kgco2e: 0.0` with an EMPTY breakdown, and that is correct. It validates
cleanly against this service's own published schema, which is how the ASHS
attribution engine exonerates cap-calc on objective evidence rather than on
the agent's opinion.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from common import disclaimer
from common.bugflags import BugFlags
from common.jsonlog import setup_logging
from common.telemetry import instrument_app, record_exception, setup_telemetry

from . import factors_client
from .calc import formulas
from .models import (
    BugFlagState,
    CalculateRequest,
    CalculateResponse,
    HealthResponse,
    TypedError,
)

SERVICE_NAME = "calc-api"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")
DEPLOYMENT_STACK = "cap-calc"
CODE_REPOSITORY = "cap-calc"

setup_logging(SERVICE_NAME, SERVICE_VERSION)
tracer = setup_telemetry(SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_STACK, CODE_REPOSITORY)
log = logging.getLogger(__name__)

BUGS = BugFlags(
    {
        "drop_breakdown_shares": (
            "Omit the `share` field from breakdown entries, violating this "
            "service's own schema. Used to prove attribution can also land here."
        ),
    }
)

app = FastAPI(
    title="cap-calc :: calc-api",
    version=SERVICE_VERSION,
    description=(
        "Household carbon footprint calculation for the ASHS demo."
        + disclaimer.OPENAPI_NOTE
    ),
)
instrument_app(app)

# In-memory calculation store. A real deployment would persist; the demo resets
# on restart by design, which is what keeps `make reset` under 30 seconds.
_calculations: dict[str, dict] = {}


@app.on_event("startup")
def _startup() -> None:
    BUGS.reset()
    log.info("calc-api ready, factor-api=%s", factors_client.FACTOR_API_URL)


def _new_id() -> str:
    return f"calc_{secrets.token_hex(8)}"


@app.post(
    "/v1/calculate",
    response_model=CalculateResponse,
    responses={400: {"model": TypedError}, 502: {"model": TypedError}},
)
def calculate(request: CalculateRequest):
    from opentelemetry import trace

    span = trace.get_current_span()
    span.set_attribute("calc.region", request.region)
    span.set_attribute("calc.period", request.period)

    activities = request.activities.model_dump()

    # One bulk lookup for exactly the factors this input needs.
    needed = formulas.required_activities(activities)
    try:
        factors = factors_client.fetch_factors(request.region, needed)
    except factors_client.FactorLookupError as exc:
        record_exception(span, exc)
        log.exception("factor lookup failed for region=%s", request.region)
        return JSONResponse(
            status_code=502,
            content=TypedError(
                error="factor_lookup_failed",
                message=str(exc),
                detail={"region": request.region, "downstream": "factor-api"},
            ).model_dump(),
        )

    try:
        categories = formulas.compute_categories(activities, factors, request.period)
    except (TypeError, ValueError, KeyError) as exc:
        # A TypeError here almost always means cap-factors sent a null or a
        # string where the contract requires a number. We surface it as a 502
        # with the downstream named, because the fault is not ours -- and the
        # span already carries the raw payload that proves it.
        record_exception(span, exc)
        span.set_attribute("calc.failure_origin", "downstream_factor_data")
        log.exception("calculation failed for region=%s", request.region)
        return JSONResponse(
            status_code=502,
            content=TypedError(
                error="invalid_factor_data",
                message=f"Emission factor data is unusable: {exc}",
                detail={"region": request.region, "downstream": "factor-api"},
            ).model_dump(),
        )

    total = sum(c.kgco2e for c in categories)
    breakdown = formulas.build_breakdown(categories, total)

    if BUGS.is_on("drop_breakdown_shares"):
        breakdown = [{k: v for k, v in e.items() if k != "share"} for e in breakdown]

    calc_id = _new_id()
    result = {
        "calculation_id": calc_id,
        "total_kgco2e": round(total, 2),
        "period": request.period,
        "breakdown": breakdown,
        "annualised_tco2e": round(formulas.annualise(total, request.period), 3),
        "factor_dataset_version": factors_client.dataset_version(request.region),
        "computed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _calculations[calc_id] = result

    span.set_attribute("calc.total_kgco2e", result["total_kgco2e"])
    span.set_attribute("calc.category_count", len(breakdown))

    # An empty breakdown is legal and correct. Log it plainly so the demo
    # narrative is visible in the telemetry, not just in the slides.
    if not breakdown:
        log.info(
            "all activity inputs zero -> total 0.0 with empty breakdown "
            "(contract-compliant)"
        )

    return JSONResponse(content=result)


@app.get(
    "/v1/calculations/{calculation_id}",
    response_model=CalculateResponse,
    responses={404: {"model": TypedError}},
)
def get_calculation(calculation_id: str):
    result = _calculations.get(calculation_id)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=TypedError(
                error="calculation_not_found",
                message=f"No calculation with id '{calculation_id}'.",
            ).model_dump(),
        )
    return JSONResponse(content=result)


@app.get("/health", response_model=HealthResponse)
def health(response: Response):
    downstream = factors_client.ping()
    ok = downstream == "ok"
    response.status_code = 200 if ok else 503
    return HealthResponse(
        status="healthy" if ok else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        dependencies={"factor-api": downstream},
    )


@app.get("/_demo/evidence")
def get_evidence():
    """Recent raw downstream response bodies, for the ASHS evidence builder."""
    return {"service": SERVICE_NAME, "evidence": factors_client.recent_evidence()}


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

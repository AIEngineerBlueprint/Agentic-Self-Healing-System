"""cfc-api -- the Backend For Frontend of the Carbon Ledger product.

Owned by the product team. The browser talks only to this service, never
directly to a capability.

=== SCENARIO 1 LIVES HERE ==================================================

`zero_total_division` reproduces a defensive-handling gap that is entirely the
product's fault:

  - The user submits all zeros.
  - calc-api correctly returns total_kgco2e 0.0 with an empty breakdown. That
    response validates cleanly against calc-api's published schema.
  - This service computes each category's percentage by dividing by the total,
    produces NaN or raises, and returns a 500.

The capability behaved correctly. The product failed to handle a legal, correct
response. Attribution should exonerate both capabilities on objective evidence
and land the fix here, with a test asserting that a zero footprint renders as a
zero state rather than an error.
============================================================================
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry import trace

from common import disclaimer
from common.bugflags import BugFlags
from common.jsonlog import setup_logging
from common.telemetry import instrument_app, record_exception, setup_telemetry

from .models import (
    BugFlagState,
    CalculateRequest,
    FootprintResponse,
    HealthResponse,
    RegionsResponse,
    TypedError,
)
from .suggestions import build_suggestions

SERVICE_NAME = "cfc-api"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")
DEPLOYMENT_STACK = "cfc-product"
CODE_REPOSITORY = "cfc-product"

# Never hardcode a service name -- one env var per cross-stack dependency.
CALC_API_URL = os.environ.get("CALC_API_URL", "http://calc-api:8080")
FACTOR_API_URL = os.environ.get("FACTOR_API_URL", "http://factor-api:8080")
TIMEOUT = float(os.environ.get("DOWNSTREAM_TIMEOUT", "8.0"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "http://localhost:3000")

setup_logging(SERVICE_NAME, SERVICE_VERSION)
tracer = setup_telemetry(SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_STACK, CODE_REPOSITORY)
log = logging.getLogger(__name__)

BUGS = BugFlags(
    {
        "zero_total_division": (
            "Compute category percentages by dividing by the total without "
            "guarding zero. A legal all-zero calculation then produces NaN or "
            "raises. Scenario 1 -- the PRODUCT owns this fault."
        ),
    }
)

app = FastAPI(
    title="cfc-product :: cfc-api",
    version=SERVICE_VERSION,
    description=(
        "Backend for frontend for the Carbon Ledger demo product."
        + disclaimer.OPENAPI_NOTE
    ),
)
instrument_app(app)

# The one restriction worth keeping: a broken CORS config is a fun bug but a
# boring one, so the frontend origin is pinned rather than wide open.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_results: dict[str, dict] = {}


@app.on_event("startup")
def _startup() -> None:
    BUGS.reset()
    log.info("cfc-api ready, calc-api=%s", CALC_API_URL)


# --------------------------------------------------------------------------
# The defect. Kept as one small, obviously-fixable function so the generated
# patch is reviewable on a projector.
# --------------------------------------------------------------------------
def compute_percentages(breakdown: list[dict], total: float) -> list[dict]:
    """Attach a display percentage to each breakdown entry.

    calc-api already supplies `share` as a 0..1 float. The product recomputes it
    as a percentage for the chart component.
    """
    if BUGS.is_on("zero_total_division"):
        # DEFECT: no guard on total == 0. A legal all-zero calculation reaches
        # here with total 0.0 and an empty breakdown; any later consumer of
        # `percent` gets NaN, and a non-empty breakdown would raise outright.
        enriched = [dict(entry, percent=round(entry["kgco2e"] / total * 100, 1)) for entry in breakdown]
        # The chart component requires a percentage on every render, including
        # the empty case -- this is where the zero state blows up.
        headline_percent = max((e["percent"] for e in enriched), default=0.0)
        _ = 100.0 / total  # noqa: F841 - explicit ZeroDivisionError on zero totals
        return enriched

    # CORRECT: a zero total is a valid zero state, not an error.
    if total <= 0:
        return [dict(entry, percent=0.0) for entry in breakdown]
    return [
        dict(entry, percent=round(entry["kgco2e"] / total * 100, 1))
        for entry in breakdown
    ]


@app.post(
    "/api/footprint/calculate",
    response_model=FootprintResponse,
    responses={400: {"model": TypedError}, 500: {"model": TypedError}, 502: {"model": TypedError}},
)
def calculate(request: CalculateRequest):
    span = trace.get_current_span()
    span.set_attribute("product.region", request.region)
    span.set_attribute("product.period", request.period)

    # --- Call the calculation capability ---------------------------------
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                f"{CALC_API_URL}/v1/calculate",
                json={
                    "period": request.period,
                    "region": request.region,
                    "activities": request.activities.model_dump(),
                },
            )
    except httpx.RequestError as exc:
        record_exception(span, exc)
        log.exception("calc-api unreachable")
        return JSONResponse(
            status_code=502,
            content=TypedError(
                error="capability_unreachable",
                message=f"The calculation service is unreachable: {exc}",
                detail={"downstream": "calc-api"},
            ).model_dump(),
        )

    raw_body = response.text

    if response.status_code != 200:
        # EVIDENCE CAPTURE: attach the raw downstream body to the span so the
        # ASHS evidence builder has the actual payload to validate.
        span.set_attribute("downstream.service", "calc-api")
        span.set_attribute("downstream.status_code", response.status_code)
        span.set_attribute("downstream.response_body", raw_body[:4000])
        log.error("calc-api returned %s: %s", response.status_code, raw_body[:500])
        return JSONResponse(
            status_code=502,
            content=TypedError(
                error="calculation_failed",
                message="The calculation service could not complete this request.",
                detail={"downstream": "calc-api", "status": response.status_code},
            ).model_dump(),
        )

    calc = response.json()
    # EVIDENCE CAPTURE on the SUCCESS path too. Attribution needs the payload
    # that was returned even when the downstream call worked -- that is exactly
    # how a capability gets exonerated on objective evidence rather than on the
    # absence of a complaint.
    span.set_attribute("downstream.service", "calc-api")
    span.set_attribute("downstream.operation", "POST /v1/calculate")
    span.set_attribute("downstream.status_code", response.status_code)
    span.set_attribute("downstream.response_body", raw_body[:4000])
    span.set_attribute("calc.total_kgco2e", calc.get("total_kgco2e", -1))
    span.set_attribute("calc.breakdown_count", len(calc.get("breakdown", [])))

    total = calc["total_kgco2e"]

    # --- Shape for the UI. This is where Scenario 1 fails. ---------------
    try:
        breakdown = compute_percentages(calc.get("breakdown", []), total)
    except ZeroDivisionError as exc:
        record_exception(span, exc)
        span.set_attribute("product.failure_origin", "cfc-api.compute_percentages")
        log.exception(
            "failed to shape breakdown for total=%s (downstream response was valid)",
            total,
        )
        return JSONResponse(
            status_code=500,
            content=TypedError(
                error="internal_error",
                message="Something went wrong preparing your results.",
            ).model_dump(),
        )

    # --- Regional context -------------------------------------------------
    region_meta = _region_meta(request.region)
    annualised = calc["annualised_tco2e"]

    result = {
        "calculation_id": calc["calculation_id"],
        "period": calc["period"],
        "region": request.region,
        "region_name": region_meta.get("name", request.region),
        "total_kgco2e": total,
        "annualised_tco2e": annualised,
        "breakdown": breakdown,
        "comparison": {
            "regional_average_tco2e": region_meta.get("avg_annual_tco2e"),
            "regional_average_source": region_meta.get("avg_source"),
            "target_tco2e": 2.3,
            "target_label": "1.5°C-aligned per-capita target by 2030",
            "vs_regional_average_pct": (
                round((annualised / region_meta["avg_annual_tco2e"] - 1) * 100, 1)
                if region_meta.get("avg_annual_tco2e")
                else None
            ),
            "vs_target_pct": round((annualised / 2.3 - 1) * 100, 1),
        },
        "suggestions": build_suggestions(breakdown, calc["period"], request.activities.model_dump()),
        "factor_dataset_version": calc["factor_dataset_version"],
        "computed_at": calc["computed_at"],
        "disclaimer": disclaimer.LONG,
    }

    _results[result["calculation_id"]] = result
    return JSONResponse(content=result)


def _region_meta(code: str) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{FACTOR_API_URL}/v1/regions")
        if r.status_code == 200:
            for region in r.json().get("regions", []):
                if region["code"] == code:
                    return region
    except httpx.RequestError:
        log.warning("could not fetch region metadata for %s", code)
    return {}


@app.get(
    "/api/footprint/{calculation_id}",
    response_model=FootprintResponse,
    responses={404: {"model": TypedError}},
)
def get_footprint(calculation_id: str):
    result = _results.get(calculation_id)
    if result is None:
        return JSONResponse(
            status_code=404,
            content=TypedError(
                error="not_found",
                message=f"No calculation with id '{calculation_id}'.",
            ).model_dump(),
        )
    return JSONResponse(content=result)


@app.get("/api/regions", response_model=RegionsResponse)
def regions():
    """Proxied from factor-api so the browser never calls a capability."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{FACTOR_API_URL}/v1/regions")
        if r.status_code == 200:
            return JSONResponse(content=r.json())
    except httpx.RequestError as exc:
        record_exception(trace.get_current_span(), exc)
    return JSONResponse(
        status_code=502,
        content=TypedError(
            error="capability_unreachable",
            message="Region list is temporarily unavailable.",
            detail={"downstream": "factor-api"},
        ).model_dump(),
    )


@app.get("/api/health", response_model=HealthResponse)
def health(response: Response):
    deps: dict[str, str] = {}
    for name, url in (("calc-api", CALC_API_URL), ("factor-api", FACTOR_API_URL)):
        try:
            with httpx.Client(timeout=2.0) as client:
                r = client.get(f"{url}/health")
            deps[name] = "ok" if r.status_code == 200 else f"unhealthy: {r.status_code}"
        except httpx.RequestError as exc:
            deps[name] = f"error: {type(exc).__name__}"

    ok = all(v == "ok" for v in deps.values())
    response.status_code = 200 if ok else 503
    return HealthResponse(
        status="healthy" if ok else "degraded",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        dependencies=deps,
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

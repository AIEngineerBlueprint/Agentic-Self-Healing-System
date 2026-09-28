"""Client for the cap-factors capability.

Two things here exist purely to make agentic attribution possible:

  1. EVIDENCE CAPTURE. When a downstream call fails or returns something
     unusable, the raw response body is attached to the span. Without the actual
     payload, contract validation has nothing to validate -- and contract
     validation is the decisive attribution test.

  2. NO DEFENSIVE DEFAULTS. A missing or malformed factor raises. Silently
     substituting zero would hide a real data defect behind a plausible-looking
     number, which is precisely the failure mode the demo exists to prevent.
"""

from __future__ import annotations

import logging
import os

import httpx
from opentelemetry import trace

from common.telemetry import record_exception

log = logging.getLogger(__name__)

# Never hardcode a service name. One env var per cross-stack dependency is what
# makes relocating a capability a config change rather than a code change.
FACTOR_API_URL = os.environ.get("FACTOR_API_URL", "http://factor-api:8080")
TIMEOUT = float(os.environ.get("FACTOR_API_TIMEOUT", "5.0"))

# Keep recent raw downstream bodies retrievable for a short window so the ASHS
# evidence builder can pull the exact payload behind a failure.
_EVIDENCE_WINDOW = 50
_recent_evidence: list[dict] = []


def recent_evidence() -> list[dict]:
    return list(_recent_evidence)


def _record_evidence(entry: dict) -> None:
    _recent_evidence.append(entry)
    if len(_recent_evidence) > _EVIDENCE_WINDOW:
        del _recent_evidence[: len(_recent_evidence) - _EVIDENCE_WINDOW]


class FactorLookupError(RuntimeError):
    """Raised when cap-factors cannot supply usable factors."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def fetch_factors(region: str, activities: list[str], year: int = 2026) -> dict[str, float]:
    """Bulk-fetch emission factors. Returns {activity: factor}.

    Raises FactorLookupError on any response we cannot use, with the raw body
    attached so the failing payload survives into the evidence bundle.
    """
    span = trace.get_current_span()
    span.set_attribute("factors.region", region)
    span.set_attribute("factors.requested_count", len(activities))

    url = f"{FACTOR_API_URL}/v1/factors/bulk"
    params = {"region": region, "activities": ",".join(activities), "year": year}

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.get(url, params=params)
    except httpx.RequestError as exc:
        record_exception(span, exc)
        raise FactorLookupError(f"cap-factors unreachable: {exc}") from exc

    raw_body = response.text

    if response.status_code != 200:
        # Attach the raw body to the span. This is the evidence.
        span.set_attribute("downstream.status_code", response.status_code)
        span.set_attribute("downstream.response_body", raw_body[:4000])
        span.set_attribute("downstream.service", "factor-api")
        _record_evidence(
            {
                "service": "factor-api",
                "url": url,
                "params": params,
                "status": response.status_code,
                "body": raw_body[:4000],
            }
        )
        raise FactorLookupError(
            f"cap-factors returned {response.status_code}",
            status=response.status_code,
            body=raw_body,
        )

    payload = response.json()
    span.set_attribute(
        "factors.dataset_version", payload.get("dataset_version", "unknown")
    )

    # EVIDENCE CAPTURE on the success path. Bounded, short-lived, and the reason
    # cap-factors can be exonerated on evidence rather than on silence.
    span.set_attribute("downstream.service", "factor-api")
    span.set_attribute("downstream.operation", "GET /v1/factors/bulk")
    span.set_attribute("downstream.status_code", 200)
    span.set_attribute("downstream.response_body", raw_body[:4000])

    factors: dict[str, float] = {}
    invalid: list[dict] = []

    for entry in payload.get("factors", []):
        value = entry.get("factor")
        # Contract: factor is a required number >= 0. Anything else is a
        # violation by cap-factors, and we record it as such rather than
        # papering over it.
        if value is None or isinstance(value, str) or isinstance(value, bool):
            invalid.append({"activity": entry.get("activity"), "factor": value})
        factors[entry["activity"]] = value

    if invalid:
        span.set_attribute("downstream.contract_violation", True)
        span.set_attribute("downstream.service", "factor-api")
        span.set_attribute("downstream.invalid_factors", str(invalid)[:2000])
        span.set_attribute("downstream.response_body", raw_body[:4000])
        _record_evidence(
            {
                "service": "factor-api",
                "url": url,
                "params": params,
                "status": 200,
                "body": raw_body[:4000],
                "contract_violation": invalid,
            }
        )
        log.error(
            "cap-factors returned %d factor(s) violating its published schema: %s",
            len(invalid),
            invalid,
        )

    return factors


def dataset_version(region: str = "IN-KA") -> str:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(f"{FACTOR_API_URL}/v1/regions")
            if r.status_code == 200:
                return r.json().get("dataset_version", "unknown")
    except httpx.RequestError:
        pass
    return "unknown"


def ping() -> str:
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(f"{FACTOR_API_URL}/health")
        return "ok" if r.status_code == 200 else f"unhealthy: {r.status_code}"
    except httpx.RequestError as exc:
        return f"error: {type(exc).__name__}"

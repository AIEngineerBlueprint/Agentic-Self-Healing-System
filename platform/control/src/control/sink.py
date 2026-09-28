"""OTLP/JSON ingest.

The OTel Collector exports here over otlphttp with JSON encoding, so this module
parses the standard OTLP JSON shape rather than a bespoke format. That matters
for migration: swapping this sink for Tempo/Loki later means changing one
exporter block in the collector config and nothing in any service.

OTLP nests attributes as [{"key": k, "value": {"<type>Value": v}}], and times as
nanosecond strings. Both are flattened here so downstream queries are ordinary
SQL and JSONB rather than protobuf archaeology.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# OTLP status codes are ints on the wire.
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
_KIND = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _value(v: dict | None) -> Any:
    """Unwrap an OTLP AnyValue into a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs(v["kvlistValue"].get("values", []))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _attrs(pairs: list[dict] | None) -> dict[str, Any]:
    """Flatten OTLP key/value pairs into a plain dict."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key = pair.get("key")
        if key:
            out[key] = _value(pair.get("value"))
    return out


def _ts(nanos: str | int | None) -> datetime:
    """OTLP unix-nano (often a string, JS-safety) -> aware datetime."""
    if nanos in (None, "", 0, "0"):
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(int(nanos) / 1_000_000_000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def parse_traces(payload: dict) -> list[dict]:
    """OTLP ExportTraceServiceRequest -> rows for tel_spans."""
    rows: list[dict] = []

    for resource_span in payload.get("resourceSpans", []):
        resource = _attrs(resource_span.get("resource", {}).get("attributes"))

        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                start = _ts(span.get("startTimeUnixNano"))
                end = _ts(span.get("endTimeUnixNano"))
                status = span.get("status", {}) or {}

                events = []
                for event in span.get("events", []) or []:
                    events.append(
                        {
                            "name": event.get("name"),
                            "time": _ts(event.get("timeUnixNano")).isoformat(),
                            "attributes": _attrs(event.get("attributes")),
                        }
                    )

                rows.append(
                    {
                        "span_id": span.get("spanId", ""),
                        "trace_id": span.get("traceId", ""),
                        "parent_span_id": span.get("parentSpanId") or None,
                        "name": span.get("name", ""),
                        "service_name": resource.get("service.name", "unknown"),
                        "service_version": resource.get("service.version"),
                        "deployment_stack": resource.get("deployment.stack"),
                        "code_repository": resource.get("code.repository"),
                        "kind": _KIND.get(span.get("kind", 0), "UNSPECIFIED"),
                        "start_time": start,
                        "end_time": end,
                        "duration_ms": round((end - start).total_seconds() * 1000, 3),
                        "status_code": _STATUS.get(status.get("code", 0), "UNSET"),
                        "status_message": status.get("message"),
                        "attributes": _attrs(span.get("attributes")),
                        "events": events,
                    }
                )

    return rows


def parse_logs(payload: dict) -> list[dict]:
    """OTLP ExportLogsServiceRequest -> rows for tel_logs."""
    rows: list[dict] = []

    for resource_log in payload.get("resourceLogs", []):
        resource = _attrs(resource_log.get("resource", {}).get("attributes"))

        for scope_log in resource_log.get("scopeLogs", []):
            for record in scope_log.get("logRecords", []):
                observed = record.get("observedTimeUnixNano") or record.get("timeUnixNano")
                rows.append(
                    {
                        "trace_id": record.get("traceId") or None,
                        "span_id": record.get("spanId") or None,
                        "service_name": resource.get("service.name", "unknown"),
                        "service_version": resource.get("service.version"),
                        "deployment_stack": resource.get("deployment.stack"),
                        "code_repository": resource.get("code.repository"),
                        "severity": record.get("severityText") or "INFO",
                        "body": _value(record.get("body")),
                        "attributes": _attrs(record.get("attributes")),
                        "observed_at": _ts(observed),
                    }
                )

    return rows

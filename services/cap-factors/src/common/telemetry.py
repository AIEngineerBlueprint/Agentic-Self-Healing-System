"""OpenTelemetry setup shared by every ASHS demo service.

Two things here are load-bearing for the agentic pipeline, not decoration:

1. `code.repository` on every span. This is what lets the supervisor map a
   failing span to a repository without maintaining a lookup table.
2. Errors recorded as span *events* with exception type and stack trace, not
   just a status code. Attribution needs the exception, not the colour.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialised = False


def setup_telemetry(
    service_name: str,
    service_version: str,
    deployment_stack: str,
    code_repository: str,
) -> trace.Tracer:
    """Wire up OTLP traces + logs. Idempotent."""
    global _initialised

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            # Custom attributes the ASHS control plane depends on.
            "deployment.stack": deployment_stack,
            "code.repository": code_repository,
        }
    )

    if not _initialised:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
                schedule_delay_millis=1000,
            )
        )
        trace.set_tracer_provider(provider)

        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"),
                schedule_delay_millis=1000,
            )
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
        logging.getLogger().addHandler(handler)

        HTTPXClientInstrumentor().instrument()
        _initialised = True

    return trace.get_tracer(service_name, service_version)


def instrument_app(app) -> None:
    """Attach FastAPI auto-instrumentation. Excludes noise endpoints."""
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="health,openapi.json,_demo/bug,metrics"
    )


def record_exception(span, exc: BaseException) -> None:
    """Record an exception as a span event with type + stack trace.

    A status code alone tells the attribution engine that something failed.
    The exception type and stack trace tell it *what* failed and *where*,
    which is what the diagnosis actually needs.
    """
    span.record_exception(exc, escaped=True)
    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
    span.set_attribute("error.type", type(exc).__name__)
    span.set_attribute("error.message", str(exc))


def current_trace_ids() -> tuple[str, str]:
    """Return (trace_id, span_id) as lowercase hex, or empty strings."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")

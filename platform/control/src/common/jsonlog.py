"""Structured JSON logging.

Required fields on every line: timestamp, level, service, version, trace_id,
span_id, message. Error lines add error.type, error.message, error.stack.

The trace_id / span_id correlation is what lets the ASHS evidence builder pull
the exact log lines for one failing request rather than a time-window guess.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone

from .telemetry import current_trace_ids


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str) -> None:
        super().__init__()
        self.service = service
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_trace_ids()
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "version": self.version,
            "trace_id": trace_id,
            "span_id": span_id,
            "message": record.getMessage(),
        }

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            payload["error.type"] = exc_type.__name__ if exc_type else "Unknown"
            payload["error.message"] = str(exc_value)
            payload["error.stack"] = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )

        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value

        return json.dumps(payload, default=str)


def setup_logging(service: str, version: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, version))

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn's own handlers would emit non-JSON lines alongside ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def log_with(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Emit a log line with extra structured fields."""
    record_extra = {"extra_fields": fields}
    logger.log(level, msg, extra=record_extra)

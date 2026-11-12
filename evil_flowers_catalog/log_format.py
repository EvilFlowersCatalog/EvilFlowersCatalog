"""Log formatters (IP-019).

`JsonFormatter` writes one JSON object per line, the shape Grafana Alloy parses
from container stdout/stderr: its `stage.json` reads exactly `level` and
`trace_id`, and Grafana links `trace_id` to the Tempo trace.

    {"time": "2026-11-06T10:15:02.123Z", "level": "INFO", "logger": "apps.mcp.server",
     "msg": "...", "trace_id": "4bf9...", "span_id": "00f0...", "license_id": "..."}

Standard library only, plus the OpenTelemetry API when it is installed. This
module must stay free of Django and of package-relative imports: gunicorn's
master process loads it by file path (`conf/gunicorn.conf.py`) so that its own
error log is JSON too, without importing the project (and with it Celery and
`ssl`) before gevent monkey-patches the workers.
"""

import json
import logging
from datetime import datetime, timezone

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace = None

# Attributes every LogRecord has. Whatever else is on a record came from
# `extra=` and is emitted as a top-level key.
_RECORD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


def current_trace_ids():
    """`(trace_id, span_id)` of the current span as lowercase hex, or `(None, None)`."""
    if trace is None:
        return None, None
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return trace.format_trace_id(span_context.trace_id), trace.format_span_id(span_context.span_id)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.upper(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload["time"] = payload["time"].replace("+00:00", "Z")

        trace_id, span_id = current_trace_ids()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            # An `extra=` key never overwrites the fields Alloy depends on.
            if key in _RECORD_ATTRIBUTES or key.startswith("_") or key in payload:
                continue
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines for a terminal (`EVILFLOWERS_LOG_FORMAT=text`)."""

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        trace_id, _ = current_trace_ids()
        return f"{line} trace_id={trace_id}" if trace_id else line


__all__ = ["JsonFormatter", "TextFormatter", "current_trace_ids"]

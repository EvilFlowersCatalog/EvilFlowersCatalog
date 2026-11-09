"""Gunicorn configuration for gevent workers."""

import importlib.util
import os

# Worker
worker_class = "gevent"
workers = 4
timeout = 240

# Bind
bind = "0.0.0.0:8000"

# Logging (IP-019)
#
# No access log: the OpenTelemetry server span and `http.server.request.duration`
# (plus Tempo's span metrics) carry method, route, status and latency, and a
# free-text access line would only be noise for Alloy's JSON parser.
accesslog = None


def _load_json_formatter():
    """Load `evil_flowers_catalog/log_format.py` by path.

    Importing it as `evil_flowers_catalog.log_format` would run the package's
    `__init__`, which imports Celery and the Django settings into this master
    process, before gevent monkey-patches the forked workers.
    """
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(project, "evil_flowers_catalog", "log_format.py")
    spec = importlib.util.spec_from_file_location("_evilflowers_log_format", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.JsonFormatter


# Gunicorn's own error log ("Booting worker", worker timeouts) as JSON lines.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": _load_json_formatter()}},
    "handlers": {
        "error_console": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stderr"},
    },
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["error_console"], "propagate": False},
        "gunicorn.access": {"level": "INFO", "handlers": [], "propagate": False},
    },
    "root": {"level": "INFO", "handlers": ["error_console"]},
}


def worker_exit(server, worker):
    """Flush telemetry and profiles in the worker process as it leaves (runs in the child)."""
    try:
        from evil_flowers_catalog import profiling, telemetry
    except Exception:  # the app may have failed to load
        return
    telemetry.shutdown()
    profiling.shutdown()

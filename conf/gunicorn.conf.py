"""Gunicorn configuration for gevent workers with OpenTelemetry/Logfire support.

The gevent worker class monkey-patches threading, which breaks OTEL's
BatchSpanProcessor. We apply gevent monkey-patching BEFORE anything else
loads, and re-initialize the logfire exporter in each worker post-fork.
"""

# Worker
worker_class = "gevent"
workers = 4
timeout = 240

# Bind
bind = "0.0.0.0:8000"


def post_fork(server, worker):
    """Re-initialize logfire in each worker process after fork.

    The OTEL BatchSpanProcessor uses background threads that don't survive
    fork(). We need to re-configure logfire so each worker gets its own
    working exporter.
    """
    import os

    if os.getenv("LOGFIRE_TOKEN"):
        try:
            import logfire

            logfire.configure(
                service_name=os.getenv("LOGFIRE_SERVICE_NAME", "evilflowers-catalog"),
                environment=os.getenv("LOGFIRE_ENVIRONMENT", "development"),
            )
        except ImportError:
            pass
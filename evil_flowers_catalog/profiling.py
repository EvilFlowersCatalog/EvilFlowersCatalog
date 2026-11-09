"""Continuous CPU profiling with the Pyroscope SDK (#111).

On k8s node `control` the eBPF profiler cannot unwind Python: the kernel
verifier rejects its Python unwinder, so its profiles of the catalog stop at
`_PyEval_EvalFrameDefault`. The SDK (`pyroscope-io`) samples the interpreter
from inside the process and reports Python frames: views, ORM, Celery tasks.

One entry point, `setup(role)`, called once per process *after* it has forked,
from the same places as `telemetry.setup()` and just before it:

- gunicorn worker: `evil_flowers_catalog/wsgi.py`
- Celery prefork child: the `worker_process_init` signal (`evil_flowers_catalog/celery.py`)
- Celery, non-forking pool: the `worker_init` signal
- Celery beat: the `beat_init` signal
- `manage.py celery_event_handler`: at the start of `handle()`

The sampler is a native thread, and threads do not survive `fork()`, so a
profiler started before the fork would profile nothing.

Configuration is the environment the k8s role sets, nothing else:

- `PYROSCOPE_SERVER_ADDRESS`: unset or empty means off.
- `PYROSCOPE_APPLICATION_NAME`: default `OTEL_SERVICE_NAME`, then `evilflowers`.
- `PYROSCOPE_TAGS`: `key=value,key=value`. A `role` tag (`gunicorn`,
  `celery-worker`, ...) is added, so the processes of one container can be told
  apart, as `evilflowers.process.role` does for the OTel resource.

`pyroscope` is imported lazily, so a deployment built without it runs unchanged.
"""

import atexit
import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_APPLICATION_NAME = "evilflowers"
ROLE_TAG = "role"
SAMPLE_RATE = 100

_lock = threading.Lock()
_state = {"role": None, "enabled": False}


def server_address() -> Optional[str]:
    return os.environ.get("PYROSCOPE_SERVER_ADDRESS", "").strip() or None


def application_name() -> str:
    for variable in ("PYROSCOPE_APPLICATION_NAME", "OTEL_SERVICE_NAME"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    return DEFAULT_APPLICATION_NAME


def parse_tags(raw: str) -> Dict[str, str]:
    """`a=1,b=2` to `{"a": "1", "b": "2"}`. Items without `=` or without a key are skipped."""
    tags = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if separator and key:
            tags[key] = value
    return tags


def build_tags(role: str) -> Dict[str, str]:
    tags = parse_tags(os.environ.get("PYROSCOPE_TAGS", ""))
    tags.setdefault(ROLE_TAG, role)
    return tags


def is_enabled() -> bool:
    return bool(_state["enabled"])


def setup(role: str) -> bool:
    """Start the profiler in this process. Idempotent.

    Returns whether profiling is on. The first call decides; later calls,
    whatever their role, return that decision. Never raises: a profiler that
    fails to start must not take the process down with it.
    """
    with _lock:
        if _state["role"] is not None:
            return _state["enabled"]
        _state["role"] = role

        address = server_address()
        if address is None:
            return False

        try:
            import pyroscope
        except ImportError as error:
            logger.warning("Pyroscope SDK is not installed (%s); profiling is off", error)
            return False

        name = application_name()
        try:
            pyroscope.configure(
                application_name=name,
                server_address=address,
                sample_rate=SAMPLE_RATE,
                oncpu=True,
                # Also sample threads that run without the GIL (psycopg, OpenSSL).
                gil_only=False,
                tags=build_tags(role),
            )
        except Exception:
            logger.warning("Pyroscope: the profiler failed to start; profiling is off", exc_info=True)
            return False

        _state["enabled"] = True
        atexit.register(shutdown)
        logger.info("Pyroscope on: role=%s application=%s server=%s", role, name, address)
        return True


def span_processor():
    """`PyroscopeSpanProcessor` for the tracer provider, or None.

    It sets `pyroscope.profile.id` on every root span and tags the samples taken
    meanwhile with the span id, which is what Grafana's trace-to-profile link
    follows. The sample tags are kept per OS thread, so the processor is only
    correct where one thread runs one root span at a time. A gevent-patched
    process (the gunicorn workers) runs many requests on one thread, and their
    tags would overwrite each other. There it is left out.
    """
    if not is_enabled() or _gevent_patched():
        return None
    try:
        from pyroscope.otel import PyroscopeSpanProcessor
    except ImportError:
        logger.info("Pyroscope: pyroscope-otel is not installed; spans are not linked to profiles")
        return None
    return PyroscopeSpanProcessor()


def _gevent_patched() -> bool:
    try:
        from gevent import monkey
    except ImportError:
        return False
    return monkey.is_module_patched("threading")


def shutdown() -> None:
    """Upload the last samples and stop the profiler. Idempotent; safe from any exit path."""
    with _lock:
        if not _state["enabled"]:
            return
        _state["enabled"] = False
    try:
        import pyroscope

        pyroscope.shutdown()
    except Exception:  # pragma: no cover - never let exit fail on profiling
        logger.debug("Pyroscope shutdown failed", exc_info=True)


__all__ = [
    "application_name",
    "build_tags",
    "is_enabled",
    "parse_tags",
    "server_address",
    "setup",
    "shutdown",
    "span_processor",
]

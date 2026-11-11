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
- `PYROSCOPE_SAMPLE_RATE`: samples per second, default 20 (#136).
- `PYROSCOPE_GIL_ONLY`: `true`/`false`. Default: `false` for gunicorn (its
  psycopg/OpenSSL time without the GIL is worth seeing), `true` for every other
  role (#136).
- `PYROSCOPE_ROLES`: comma-separated roles that are profiled, default
  `gunicorn,celery-worker`; `*` profiles every role. Beat and the event
  handler are idle nearly always and don't pay for a sampler by default (#136).

The cost is per process: at 100 Hz with `gil_only=False` the sampler thread
kept ~0.1 core busy in every idle process, 18 of them in one pod (#136).

`pyroscope` is imported lazily, so a deployment built without it runs unchanged.
"""

import atexit
import logging
import os
import threading
from typing import Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

DEFAULT_APPLICATION_NAME = "evilflowers"
ROLE_TAG = "role"
DEFAULT_SAMPLE_RATE = 20
MAX_SAMPLE_RATE = 1000
DEFAULT_ROLES = frozenset({"gunicorn", "celery-worker"})
ALL_ROLES = "*"
# Roles that sample threads without the GIL too, unless PYROSCOPE_GIL_ONLY says otherwise.
ALL_THREADS_ROLES = frozenset({"gunicorn"})
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

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


def sample_rate() -> int:
    """`PYROSCOPE_SAMPLE_RATE` in Hz, within 1..MAX_SAMPLE_RATE; the default when unset or invalid."""
    raw = os.environ.get("PYROSCOPE_SAMPLE_RATE", "").strip()
    if not raw:
        return DEFAULT_SAMPLE_RATE
    try:
        rate = int(raw)
    except ValueError:
        logger.warning("PYROSCOPE_SAMPLE_RATE=%r is not an integer; using %s Hz", raw, DEFAULT_SAMPLE_RATE)
        return DEFAULT_SAMPLE_RATE
    return min(max(rate, 1), MAX_SAMPLE_RATE)


def gil_only(role: str) -> bool:
    """`PYROSCOPE_GIL_ONLY` when set to a boolean; otherwise False for gunicorn, True elsewhere."""
    raw = os.environ.get("PYROSCOPE_GIL_ONLY", "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    if raw:
        logger.warning("PYROSCOPE_GIL_ONLY=%r is not a boolean; using the role default", raw)
    return role not in ALL_THREADS_ROLES


def profiled_roles() -> Optional[FrozenSet[str]]:
    """Roles from `PYROSCOPE_ROLES`, or None for `*` (every role)."""
    raw = os.environ.get("PYROSCOPE_ROLES", "").strip()
    if not raw:
        return DEFAULT_ROLES
    roles = frozenset(item.strip() for item in raw.split(",") if item.strip())
    if ALL_ROLES in roles:
        return None
    return roles


def role_is_profiled(role: str) -> bool:
    roles = profiled_roles()
    return roles is None or role in roles


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

        if not role_is_profiled(role):
            logger.info("Pyroscope off for role=%s (not in PYROSCOPE_ROLES)", role)
            return False

        try:
            import pyroscope
        except ImportError as error:
            logger.warning("Pyroscope SDK is not installed (%s); profiling is off", error)
            return False

        name = application_name()
        rate = sample_rate()
        only_gil = gil_only(role)
        try:
            pyroscope.configure(
                application_name=name,
                server_address=address,
                sample_rate=rate,
                oncpu=True,
                # False also samples threads that run without the GIL (psycopg, OpenSSL).
                gil_only=only_gil,
                tags=build_tags(role),
            )
        except Exception:
            logger.warning("Pyroscope: the profiler failed to start; profiling is off", exc_info=True)
            return False

        _state["enabled"] = True
        atexit.register(shutdown)
        logger.info(
            "Pyroscope on: role=%s application=%s server=%s sample_rate=%s gil_only=%s",
            role,
            name,
            address,
            rate,
            only_gil,
        )
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
    "gil_only",
    "is_enabled",
    "parse_tags",
    "profiled_roles",
    "role_is_profiled",
    "sample_rate",
    "server_address",
    "setup",
    "shutdown",
    "span_processor",
]

"""OpenTelemetry for the catalog (IP-019).

One entry point, `setup(role)`, called once per process *after* it has forked
(and, for gunicorn, after gevent has monkey-patched it):

- gunicorn worker: `evil_flowers_catalog/wsgi.py`, before `get_wsgi_application()`
- Celery prefork child: the `worker_process_init` signal (`evil_flowers_catalog/celery.py`)
- Celery beat: the `beat_init` signal
- `manage.py celery_event_handler`: at the start of `handle()`

`profiling.setup(role)` (the Pyroscope SDK, #111) runs at each of these just
before, so `_link_profiles` can add its span processor.

Configuration is the standard `OTEL_*` environment and nothing else. The only
thing this module adds is *where the collector is*: when no endpoint is
configured it looks for the node's Grafana Alloy agent at the container's
default gateway (`discover_endpoint`). With no endpoint and nothing found,
telemetry stays off and the process behaves as if this module did not exist.

Logs are deliberately not exported over OTLP. Alloy already tails the
container's stdout/stderr (JSON lines, see `log_format.py`); exporting them as
well would put every line into Loki twice.

Everything below imports OpenTelemetry lazily, so a deployment built without
the `otel` dependency group runs unchanged.
"""

import atexit
import logging
import os
import signal
import socket
import struct
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Iterator, List, Optional

try:
    from opentelemetry import context as otel_context
    from opentelemetry import metrics, trace
except ImportError:  # pragma: no cover - the API ships with the base dependencies
    otel_context = None
    metrics = None
    trace = None

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "evilflowers"
ROLE_ATTRIBUTE = "evilflowers.process.role"
ROLES = ("gunicorn", "celery-worker", "celery-beat", "event-handler")

OTLP_HTTP_PORT = 4318
PROBE_TIMEOUT_SECONDS = 0.3
ROUTE_FILE = "/proc/net/route"

# Set in `os.environ` once a process has tried to discover the collector, so a
# forked child (Celery prefork) inherits the verdict instead of probing and
# logging again.
_DISCOVERY_MARKER = "EVILFLOWERS_OTEL_DISCOVERY_DONE"

ENDPOINT_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)

# Defaults this module puts into the environment when the operator has not set
# them. `setdefault` only: an explicit value always wins.
ENVIRONMENT_DEFAULTS = {
    "OTEL_SERVICE_NAME": DEFAULT_SERVICE_NAME,
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    # `http`: `http.server.request.duration` in seconds with `http.route`.
    # `database`: `db.system.name` / `server.address` on psycopg and redis
    # spans, which is what Tempo's service graph keys its virtual nodes on.
    "OTEL_SEMCONV_STABILITY_OPT_IN": "http,database",
    # The Docker HEALTHCHECK calls this every 30 s.
    "OTEL_PYTHON_DJANGO_EXCLUDED_URLS": "api/v1/status",
    # Header capture is an allow-list. Nothing that carries a credential.
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": "mcp-protocol-version,accept-language",
    # Defence in depth if an operator widens the allow-list above.
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS": (
        "authorization,proxy-authorization,cookie,set-cookie,x-api-key,.*token.*,.*secret.*,.*password.*"
    ),
    "OTEL_EXPERIMENTAL_RESOURCE_DETECTORS": "process,os,host,containerid",
}

# Process-level metrics only. Container CPU and memory come from cAdvisor and
# CPU profiles from Pyroscope; what neither can do is tell the gunicorn workers,
# the Celery children and beat apart inside one container.
SYSTEM_METRICS_CONFIG = {
    "process.cpu.time": ["user", "system"],
    "process.memory.usage": None,
    "process.thread.count": None,
    "process.open_file_descriptor.count": None,
    "cpython.gc.collections": None,
}

_lock = threading.Lock()
_state = {"role": None, "enabled": False}
_providers: List[object] = []


# -- environment ------------------------------------------------------------


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def _env_false(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"false", "0", "no", "off"}


def sdk_disabled() -> bool:
    return _env_true("OTEL_SDK_DISABLED")


def configured_endpoint() -> Optional[str]:
    for name in ENDPOINT_VARIABLES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _exporter_names(variable: str) -> List[str]:
    """`OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER`, default `otlp`."""
    raw = os.environ.get(variable, "otlp")
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    return [name for name in names if name != "none"]


# -- collector discovery ----------------------------------------------------


def parse_default_gateway(route_table: str) -> Optional[str]:
    """Return the IPv4 default gateway from the text of `/proc/net/route`.

    Columns are `Iface Destination Gateway Flags ...`, addresses in
    little-endian hex. The default route has destination `00000000` and the
    RTF_GATEWAY flag (0x2) set.
    """
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        destination, gateway, flags = fields[1], fields[2], fields[3]
        try:
            if destination != "00000000" or not int(flags, 16) & 0x2:
                continue
            address = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
        except (ValueError, struct.error, OSError):
            continue
        if address != "0.0.0.0":
            return address
    return None


def default_gateway(route_file: str = ROUTE_FILE) -> Optional[str]:
    try:
        with open(route_file) as handle:
            return parse_default_gateway(handle.read())
    except OSError:
        return None


def probe(host: str, port: int = OTLP_HTTP_PORT, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """One TCP connect, nothing sent. True when something listens there."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _endpoint_for(host: str) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{OTLP_HTTP_PORT}"


def discover_endpoint() -> Optional[str]:
    """Find the OTLP endpoint, exporting it into `os.environ` when discovered.

    Precedence: an explicit `OTEL_EXPORTER_OTLP_*ENDPOINT`; then `HOST_IP`
    (the Kubernetes downward API, `status.hostIP`); then the default gateway
    from `/proc/net/route`, which on a Docker bridge network is the host, where
    Alloy listens on the host network. Inside a Kubernetes pod the default
    gateway is the CNI's router, not the node, which is why `HOST_IP` comes first.

    Probes at most once per process tree and logs the outcome once.
    """
    explicit = configured_endpoint()
    if explicit:
        return explicit
    if os.environ.get(_DISCOVERY_MARKER):
        return None
    if _env_false("OTEL_EXPORTER_OTLP_AUTODISCOVER"):
        return None

    os.environ[_DISCOVERY_MARKER] = "1"

    host = os.environ.get("HOST_IP", "").strip() or default_gateway()
    if not host:
        logger.info("OpenTelemetry: no OTLP endpoint configured and no default gateway found; telemetry is off")
        return None

    endpoint = _endpoint_for(host)
    if not probe(host):
        logger.info("OpenTelemetry: no collector answered at %s; telemetry is off", endpoint)
        return None

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    os.environ.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    logger.info("OpenTelemetry: discovered collector at %s", endpoint)
    return endpoint


# -- setup ------------------------------------------------------------------


def is_enabled() -> bool:
    return bool(_state["enabled"])


def current_role() -> Optional[str]:
    return _state["role"]


def setup(role: str) -> bool:
    """Configure tracing and metrics for this process. Idempotent.

    Returns whether telemetry is on. The first call decides; later calls,
    whatever their role, return that decision.
    """
    with _lock:
        if _state["role"] is not None:
            return _state["enabled"]
        _state["role"] = role

        if sdk_disabled():
            return False

        endpoint = discover_endpoint()
        span_exporters = _exporter_names("OTEL_TRACES_EXPORTER")
        metric_exporters = _exporter_names("OTEL_METRICS_EXPORTER")
        if endpoint is None:
            # Without an endpoint an OTLP exporter would retry against
            # localhost:4318 forever. Only the console exporters remain.
            span_exporters = [name for name in span_exporters if name != "otlp"]
            metric_exporters = [name for name in metric_exporters if name != "otlp"]
        if not span_exporters and not metric_exporters:
            return False

        try:
            _configure(role, span_exporters, metric_exporters)
        except ImportError as error:
            logger.warning("OpenTelemetry SDK is not installed (%s); telemetry is off", error)
            return False

        _state["enabled"] = True
        return True


def _configure(role: str, span_exporters: List[str], metric_exporters: List[str]) -> None:
    for name, value in ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    resource = build_resource(role)

    tracer_provider = TracerProvider(resource=resource)
    for processor in _span_processors(span_exporters):
        tracer_provider.add_span_processor(processor)
    _link_profiles(tracer_provider)
    trace.set_tracer_provider(tracer_provider)

    # The SDK's default exemplar filter is `trace_based`, so histogram points
    # recorded inside a sampled span carry its trace id to Mimir.
    meter_provider = MeterProvider(resource=resource, metric_readers=_metric_readers(metric_exporters))
    metrics.set_meter_provider(meter_provider)

    _providers[:] = [tracer_provider, meter_provider]
    atexit.register(shutdown)

    _instrument_libraries()
    logger.info(
        "OpenTelemetry on: role=%s traces=%s metrics=%s",
        role,
        ",".join(span_exporters) or "none",
        ",".join(metric_exporters) or "none",
    )


def build_resource(role: str):
    """The process resource: detectors, then our defaults, then the environment.

    `Resource.create(attributes)` lets code win over `OTEL_RESOURCE_ATTRIBUTES`,
    so defaults the operator already set in the environment are left out.

    `service.instance.id` is `<hostname>-<pid>`: a container runs several
    processes that export the same metric series, and Mimir needs them apart.
    It replaces the SDK's random per-process UUID with something an operator
    can match against `ps` and the Pyroscope process label. Safe because
    `setup()` runs after the last fork; the SDK re-detects the id only on a
    fork that happens after the providers exist.
    """
    from django.conf import settings
    from opentelemetry.sdk.resources import OTELResourceDetector, Resource

    for name, value in ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)

    from_environment = OTELResourceDetector().detect().attributes
    defaults = {
        "service.name": DEFAULT_SERVICE_NAME,
        "service.version": getattr(settings, "VERSION", "unknown"),
        "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
        ROLE_ATTRIBUTE: role,
    }
    return Resource.create({key: value for key, value in defaults.items() if key not in from_environment})


def _span_processors(names: List[str]):
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    processors = []
    for name in names:
        if name == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            processors.append(BatchSpanProcessor(OTLPSpanExporter()))
        elif name == "console":
            processors.append(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            logger.warning("OpenTelemetry: unsupported traces exporter %r ignored", name)
    return processors


def _link_profiles(tracer_provider) -> None:
    """Root spans carry `pyroscope.profile.id` when the Pyroscope SDK runs here (#111).

    Needs `profiling.setup()` to have run first in this process.
    """
    from evil_flowers_catalog import profiling

    processor = profiling.span_processor()
    if processor is not None:
        tracer_provider.add_span_processor(processor)


def _metric_readers(names: List[str]):
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader

    readers = []
    for name in names:
        if name == "otlp":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
        elif name == "console":
            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
        else:
            logger.warning("OpenTelemetry: unsupported metrics exporter %r ignored", name)
    return readers


def _instrument_libraries() -> None:
    """Client and messaging libraries, in every role.

    The Celery instrumentation is needed on both sides: the gunicorn worker
    creates the PRODUCER span and puts the context into the task headers, the
    Celery child creates the CONSUMER span from them.
    """
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

    PsycopgInstrumentor().instrument(enable_commenter=False)
    RedisInstrumentor().instrument()
    CeleryInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    SystemMetricsInstrumentor(config=SYSTEM_METRICS_CONFIG).instrument()
    _instrument_kafka()


def _instrument_kafka() -> None:
    """kafka-python, only when the Kafka event executor is the one configured."""
    from django.conf import settings

    executor = getattr(settings, "EVILFLOWERS_EVENT_BROKER_EXECUTOR", None) or ""
    if not executor.endswith("KafkaEventExecutionService"):
        return
    try:
        from opentelemetry.instrumentation.kafka import KafkaInstrumentor
    except ImportError:
        logger.info("OpenTelemetry: Kafka executor configured but the kafka-python instrumentation is not installed")
        return
    KafkaInstrumentor().instrument()


SUPPRESS_MIDDLEWARE = "evil_flowers_catalog.telemetry.SuppressExcludedUrlsMiddleware"


def instrument_django() -> None:
    """Insert the OpenTelemetry middleware. Must run before the WSGI handler is built."""
    if not is_enabled():
        return
    from django.conf import settings
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    DjangoInstrumentor().instrument(is_sql_commentor_enabled=False)
    # Outermost, so it also wraps the OpenTelemetry middleware DjangoInstrumentor
    # just put first.
    if SUPPRESS_MIDDLEWARE not in settings.MIDDLEWARE:
        settings.MIDDLEWARE = [SUPPRESS_MIDDLEWARE, *settings.MIDDLEWARE]


class SuppressExcludedUrlsMiddleware:
    """No telemetry at all for `OTEL_PYTHON_DJANGO_EXCLUDED_URLS`.

    The Django instrumentation skips the server span of an excluded URL, but
    the queries the view runs would still be exported, each as a root trace of
    its own. `/api/v1/status` counts four tables on every Docker HEALTHCHECK,
    so that would be four orphan traces every 30 s per container.
    """

    def __init__(self, get_response):
        from opentelemetry.util.http import get_excluded_urls

        self.get_response = get_response
        self._excluded = get_excluded_urls("DJANGO")

    def __call__(self, request):
        if self._excluded and self._excluded.url_disabled(request.build_absolute_uri("?")):
            from opentelemetry.instrumentation.utils import suppress_instrumentation

            with suppress_instrumentation():
                return self.get_response(request)
        return self.get_response(request)


def shutdown() -> None:
    """Flush and stop the providers. Idempotent; safe to call from any exit path."""
    with _lock:
        providers, _providers[:] = list(_providers), []
    for provider in providers:
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - never let exit fail on telemetry
            logger.debug("OpenTelemetry provider shutdown failed", exc_info=True)


def exit_on_sigterm() -> None:
    """Turn SIGTERM into `SystemExit`, so `atexit` flushes telemetry.

    Only for processes that leave SIGTERM at its default (the event handler);
    gunicorn and Celery own the signal and are flushed through their own hooks.
    """
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return

    def _exit(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _exit)


# -- helpers for hand-made telemetry ----------------------------------------


def get_tracer(name: str):
    return trace.get_tracer(name)


def get_meter(name: str):
    return metrics.get_meter(name)


def set_span_attributes(**attributes) -> None:
    """Set attributes on the current span; a no-op without a recording span.

    Keyword names use `__` for `.`: `lcp__content_id` sets `lcp.content_id`.
    `None` values are skipped.
    """
    if trace is None:
        return
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for name, value in attributes.items():
        if value is not None:
            span.set_attribute(name.replace("__", "."), value)


@contextmanager
def suppress_http_spans():
    """Run a block without HTTP client spans (one CLIENT span per remote call)."""
    try:
        from opentelemetry.instrumentation.utils import suppress_http_instrumentation
    except ImportError:
        yield
        return
    with suppress_http_instrumentation():
        yield


class ContextBoundIterator:
    """Iterate under the context that was current when this object was made.

    A `StreamingHttpResponse` body is consumed after the view has returned and
    after the OpenTelemetry middleware has ended the server span and detached
    its context. Without this, every span created while streaming (database
    queries, LLM calls) would start a new trace. The context is attached around
    each `next()` only, never across a `yield`, so it cannot leak into another
    request served by the same worker.
    """

    def __init__(self, iterable: Iterable):
        self._iterator = iter(iterable)
        self._context = otel_context.get_current() if otel_context is not None else None

    def __iter__(self) -> Iterator:
        return self

    def __next__(self):
        if self._context is None:
            return next(self._iterator)
        token = otel_context.attach(self._context)
        try:
            return next(self._iterator)
        finally:
            otel_context.detach(token)

    def close(self) -> None:
        close = getattr(self._iterator, "close", None)
        if close is None:
            return
        if self._context is None:
            close()
            return
        token = otel_context.attach(self._context)
        try:
            close()
        finally:
            otel_context.detach(token)


def on_commit(callback) -> None:
    """Record after the surrounding transaction commits (immediately outside one).

    A counter bumped inside a transaction that later rolls back would count
    something that never happened.
    """
    from django.db import transaction

    transaction.on_commit(callback)


class Stopwatch:
    """`time.perf_counter()` in seconds, for duration histograms."""

    def __init__(self):
        self._started = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return max(time.perf_counter() - self._started, 0.0)


__all__ = [
    "ContextBoundIterator",
    "Stopwatch",
    "build_resource",
    "current_role",
    "default_gateway",
    "discover_endpoint",
    "exit_on_sigterm",
    "get_meter",
    "get_tracer",
    "instrument_django",
    "is_enabled",
    "on_commit",
    "parse_default_gateway",
    "probe",
    "set_span_attributes",
    "setup",
    "shutdown",
    "suppress_http_spans",
]

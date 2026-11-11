"""Semantic tracing: the catalog's span vocabulary and the helpers that apply it (IP-029).

`telemetry.py` (IP-019) decides *whether* a process traces and wires the
auto-instrumentation: a SERVER span per request, CLIENT spans per query, Celery
PRODUCER/CONSUMER pairs. What those spans cannot say is *what* the request was
about. This module adds that:

- **Identifiers** (`ATTRIBUTES`, `set_route_attributes`). The ids a request is
  about go onto its server span, under the keys the extraction services already
  emit (`evilflowers.extraction.run_id`, `evilflowers.acquisition_id`), so one
  TraceQL query on an id finds every span of every service that touched it.
  Ids are attributes, never part of a span name: names stay a closed set.
- **Domain spans** (`span`, `traced`). INTERNAL spans at the choke points that
  do I/O or several queries (authentication, search channels, LCP licence
  operations, notifications), so a trace reads as a hierarchy of steps rather
  than a flat list of SQL statements under one request.
- **Client spans** for the network calls no instrumentation library covers
  (`client_span`): LDAP, SMTP and `urllib`.
- **Links** (`root_span`). Batch work (a backfill of hundreds of PDFs) gives
  each item its own trace with a link back to the batch, instead of one trace
  holding hundreds of multi-minute extraction pipelines.
- **Commands** (`TracedCommand`). `manage.py` commands that do real work trace
  under the role `command`; before this they ran untraced, and every analyzer
  task they queued started a trace of its own with no cause.

The rule for hierarchy versus links: a *child* span when the work is the direct
continuation of its parent (request → Celery task → service callbacks); a
*link* for batch fan-out, re-queues, and scheduled work on a record an earlier
trace created.

Privacy: no usernames, e-mail addresses, search text, titles or chat content
go into attributes. Ids are UUIDs. The reader's own id is opt-in
(`EVILFLOWERS_TRACE_ENDUSER`).

Every helper is a cheap no-op when the process does not trace: the
OpenTelemetry API without a provider records nothing, and `span()` checks for a
recording parent before it does any work of its own.
"""

import functools
import logging
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Mapping, Optional, Sequence

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind, StatusCode

logger = logging.getLogger(__name__)

TRACER_NAME = "evilflowers"

# -- the vocabulary ---------------------------------------------------------
#
# Keys shared with the extraction services (analyzer, image, equation, text)
# are spelled exactly as they emit them.

CATALOG_ID = "evilflowers.catalog.id"
CATALOG_NAME = "evilflowers.catalog.url_name"
ENTRY_ID = "evilflowers.entry.id"
ACQUISITION_ID = "evilflowers.acquisition_id"
USER_ACQUISITION_ID = "evilflowers.user_acquisition.id"
FEED_ID = "evilflowers.feed.id"
FEED_NAME = "evilflowers.feed.url_name"
EXTRACTION_RUN_ID = "evilflowers.extraction.run_id"
EXTRACTION_JOB = "evilflowers.extraction.job"
EXTRACTION_REGION_ID = "evilflowers.extraction.region_id"
EXTRACTION_PIPELINE_VERSION = "evilflowers.extraction.pipeline_version"
LICENSE_ID = "evilflowers.readium.license.id"
RESERVATION_ID = "evilflowers.readium.reservation.id"
LCP_CONTENT_ID = "lcp.content_id"
CHAT_ID = "evilflowers.assistant.chat.id"
ANNOTATION_ID = "evilflowers.annotation.id"
CATEGORY_ID = "evilflowers.category.id"
AUTHOR_ID = "evilflowers.author.id"
REVIEW_ID = "evilflowers.review.id"

AUTH_METHOD = "evilflowers.auth.method"
AUTH_TOKEN_TYPE = "evilflowers.auth.token_type"
AUTH_OUTCOME = "evilflowers.auth.outcome"
AUTH_SUPERUSER = "evilflowers.auth.superuser"
API_KEY_ID = "evilflowers.api_key.id"
ENDUSER_ID = "enduser.id"

#: URL keyword → attribute. Routes that address a person (`user_id`, `contact_id`,
#: `api_key_id`, `shelf_record_id`) are deliberately absent.
ROUTE_ATTRIBUTES: Mapping[str, str] = {
    "catalog_id": CATALOG_ID,
    "catalog_name": CATALOG_NAME,
    "entry_id": ENTRY_ID,
    "acquisition_id": ACQUISITION_ID,
    "user_acquisition_id": USER_ACQUISITION_ID,
    "feed_id": FEED_ID,
    "feed_name": FEED_NAME,
    "run_id": EXTRACTION_RUN_ID,
    "job": EXTRACTION_JOB,
    "region_id": EXTRACTION_REGION_ID,
    "license_id": LICENSE_ID,
    "reservation_id": RESERVATION_ID,
    "chat_id": CHAT_ID,
    "annotation_id": ANNOTATION_ID,
    "category_id": CATEGORY_ID,
    "author_id": AUTHOR_ID,
    "review_id": REVIEW_ID,
}


#: What the helpers yield outside a trace; `set_attribute` on it is a no-op.
INVALID_SPAN = trace.INVALID_SPAN


def _tracer():
    # Resolved per span, not cached at import: a module-level ProxyTracer binds
    # to the first provider it sees, which makes the provider impossible to
    # swap in tests and is no faster in production (the SDK lookup is a dict).
    return trace.get_tracer(TRACER_NAME)


def clean(attributes: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """Drop `None`s and stringify what OpenTelemetry would reject (UUIDs, enums)."""
    result = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        else:
            # `str()` of a Django TextChoices member is its value; of a UUID, the canonical form.
            result[key] = str(value)
    return result


def is_recording() -> bool:
    return trace.get_current_span().is_recording()


def set_attributes(attributes: Mapping[str, object]) -> None:
    """Set attributes on the current span; a no-op without a recording span."""
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attributes(clean(attributes))


def set_route_attributes(view_kwargs: Mapping[str, object]) -> None:
    """The ids a request is about, from its URL keywords, onto the current (server) span."""
    set_attributes({ROUTE_ATTRIBUTES[name]: value for name, value in view_kwargs.items() if name in ROUTE_ATTRIBUTES})


class RouteAttributesMiddleware:
    """Puts the ids in the resolved URL onto the request's server span.

    `process_view` runs after URL resolution and inside the OpenTelemetry
    middleware's span, which is still the current span there. Inserted by
    `telemetry.instrument_django()`, so it exists only when the process traces.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    @staticmethod
    def process_view(request, view_func, view_args, view_kwargs):
        if view_kwargs:
            set_route_attributes(view_kwargs)
        return None


# -- outcomes: an expected refusal is not a fault ---------------------------


def _is_client_error(error: BaseException) -> bool:
    """A 4xx the API turns into a response (an expired token, a full queue): not a server fault (IP-019 Q4)."""
    from apps.core.errors import ProblemDetailException

    if isinstance(error, ProblemDetailException):
        status = getattr(error, "status", None)
        return status is not None and int(status) < 500
    return False


def _end_with_error(current, error: BaseException, expected: Sequence[type]) -> None:
    current.set_attribute("error.type", type(error).__qualname__)
    if isinstance(error, tuple(expected)) or _is_client_error(error):
        # Recorded, so the trace says why; not an ERROR, so span metrics stay honest.
        current.add_event("exception", {"exception.type": type(error).__qualname__})
        return
    current.record_exception(error)
    current.set_status(StatusCode.ERROR, str(error)[:200])


# -- spans ------------------------------------------------------------------


@contextmanager
def span(
    name: str,
    attributes: Optional[Mapping[str, object]] = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    links: Sequence[Link] = (),
    expected: Sequence[type] = (),
    require_parent: bool = True,
) -> Iterator[trace.Span]:
    """An as-current span, ended on exit.

    `require_parent` (the default) makes a domain span only inside a trace that
    is already recorded: a domain step on its own is noise, and outside any
    trace (a test, an untraced shell) this is a no-op that costs one lookup.

    An exception of a type in `expected`, or a 4xx `ProblemDetailException`, is
    noted on the span but does not mark it ERROR; anything else does.
    """
    if require_parent and not is_recording():
        yield trace.INVALID_SPAN
        return
    with _tracer().start_as_current_span(
        name,
        kind=kind,
        attributes=clean(attributes),
        links=list(links),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as error:
            if not isinstance(error, (GeneratorExit, KeyboardInterrupt, SystemExit)):
                _end_with_error(current, error, expected)
            raise


def traced(
    name: str,
    attributes: Optional[Callable[..., Mapping[str, object]]] = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    expected: Sequence[type] = (),
):
    """Decorator form of `span`. `attributes(*args, **kwargs)` derives the attributes from the call."""

    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if not is_recording():
                return function(*args, **kwargs)
            try:
                values = attributes(*args, **kwargs) if attributes else None
            except Exception:  # pragma: no cover - telemetry never breaks the call
                logger.debug("tracing: attributes for %s failed", name, exc_info=True)
                values = None
            with span(name, values, kind=kind, expected=expected):
                return function(*args, **kwargs)

        return wrapper

    return decorate


@contextmanager
def root_span(
    name: str,
    attributes: Optional[Mapping[str, object]] = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    expected: Sequence[type] = (),
) -> Iterator[trace.Span]:
    """A new trace for one item of batch work, linked to the span that started the batch.

    Everything the item causes (Celery tasks, the services' callbacks) lands in
    the item's own trace; the link leads from it back to the batch. The other
    direction is a query, `{ link:traceID = "<batch trace id>" }`, not an event
    on the batch span: the SDK keeps at most 128 events per span, and a
    backfill has hundreds of items. Without a recording batch span this is a
    no-op.
    """
    parent = trace.get_current_span()
    if not parent.is_recording():
        yield trace.INVALID_SPAN
        return
    parent_context = parent.get_span_context()
    token = otel_context.attach(trace.set_span_in_context(trace.INVALID_SPAN, otel_context.Context()))
    try:
        with span(
            name,
            attributes,
            kind=kind,
            links=[Link(parent_context, {"evilflowers.link.kind": "batch"})],
            expected=expected,
            require_parent=False,
        ) as item:
            yield item
    finally:
        otel_context.detach(token)


@contextmanager
def client_span(
    name: str,
    *,
    system: str,
    server_address: Optional[str],
    server_port: Optional[int] = None,
    attributes: Optional[Mapping[str, object]] = None,
    expected: Sequence[type] = (),
) -> Iterator[trace.Span]:
    """A CLIENT span for a remote call no instrumentation library sees.

    `server.address` is what Tempo's service graph keys the remote node on, so
    LDAP and SMTP appear on it next to PostgreSQL and Redis.
    """
    with span(
        name,
        {
            "server.address": server_address,
            "server.port": server_port,
            "evilflowers.client.system": system,
            **(attributes or {}),
        },
        kind=SpanKind.CLIENT,
        expected=expected,
    ) as current:
        yield current


# -- who is calling ----------------------------------------------------------


def set_caller(request, method: Optional[str]) -> None:
    """Credential kind and service identity on the request's server span; the reader only when enabled."""
    current = trace.get_current_span()
    if not current.is_recording():
        return
    from django.conf import settings

    user = getattr(request, "user", None)
    authenticated = bool(getattr(user, "is_authenticated", False))
    api_key = getattr(request, "api_key", None)
    values = {
        AUTH_METHOD: method or "none",
        AUTH_TOKEN_TYPE: getattr(request, "auth_token_type", None),
        AUTH_SUPERUSER: bool(getattr(user, "is_superuser", False)) if authenticated else None,
        API_KEY_ID: getattr(api_key, "pk", None),
    }
    if authenticated and getattr(settings, "EVILFLOWERS_TRACE_ENDUSER", False):
        values[ENDUSER_ID] = getattr(user, "pk", None)
    current.set_attributes(clean(values))


# -- management commands ----------------------------------------------------


class TracedCommandMixin:
    """Trace a `manage.py` command run from the command line, under the role `command`.

    Only `run_from_argv` (the CLI path) sets telemetry up; `call_command`, which
    tests and other commands use, goes through `execute` and stays untouched, so
    a test never decides a process's telemetry. The command runs inside one
    root span `command <name>`; a command that loops over records opens a
    `root_span` per record, linked to it. Telemetry is flushed before the
    process exits.
    """

    def run_from_argv(self, argv):
        from evil_flowers_catalog import profiling, telemetry

        name = argv[1] if len(argv) > 1 else type(self).__module__.rsplit(".", 1)[-1]
        profiling.setup("command")
        telemetry.setup("command")
        telemetry.exit_on_sigterm()
        try:
            with span(f"command {name}", {"evilflowers.command.name": name}, require_parent=False) as current:
                try:
                    return super().run_from_argv(argv)
                except SystemExit as exit_:
                    # Django turns a `CommandError` into `sys.exit(returncode)`; `span()` lets SystemExit
                    # through untouched, so a failed command is marked here.
                    if exit_.code not in (None, 0):
                        current.set_attribute("process.exit.code", exit_.code if isinstance(exit_.code, int) else 1)
                        current.set_status(StatusCode.ERROR, "command exited non-zero")
                    raise
        finally:
            telemetry.shutdown()


__all__ = [
    "ROUTE_ATTRIBUTES",
    "TracedCommandMixin",
    "clean",
    "client_span",
    "is_recording",
    "root_span",
    "set_attributes",
    "set_caller",
    "set_route_attributes",
    "span",
    "traced",
]

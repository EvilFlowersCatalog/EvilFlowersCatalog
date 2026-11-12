"""IP-029: semantic spans, route ids, links for batch work, client spans and traced commands.

Every test swaps an in-memory provider in for the global one and asserts the
*shape* of what was recorded: which span is whose parent, which trace a batch
item landed in, what is an error and what is not. `SimpleTestCase` unless a
test needs the database.
"""

import uuid
from contextlib import contextmanager
from http import HTTPStatus
from unittest import mock

import ldap
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, override_settings
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from apps.core.errors import ProblemDetailException
from evil_flowers_catalog import tracing


@contextmanager
def recorded():
    """The global tracer provider replaced by an in-memory one for the block; yields the exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with mock.patch.object(trace, "_TRACER_PROVIDER", provider):
        yield exporter
    provider.shutdown()


@contextmanager
def request_span(name="GET api/v1/test"):
    """A recording SERVER span standing in for the Django instrumentation's."""
    with tracing._tracer().start_as_current_span(name, kind=SpanKind.SERVER) as server:
        yield server


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


class SpanTests(SimpleTestCase):
    def test_no_span_outside_a_trace(self):
        with recorded() as exporter:
            with tracing.span("search") as current:
                self.assertFalse(current.is_recording())
        self.assertEqual(exporter.get_finished_spans(), ())

    def test_child_of_the_current_span_with_clean_attributes(self):
        entry_id = uuid.uuid4()
        with recorded() as exporter:
            with request_span():
                with tracing.span("search", {tracing.ENTRY_ID: entry_id, "skipped": None, "count": 3}):
                    pass
        spans = _by_name(exporter)
        self.assertEqual(spans["search"].parent.span_id, spans["GET api/v1/test"].context.span_id)
        self.assertEqual(spans["search"].attributes[tracing.ENTRY_ID], str(entry_id))
        self.assertEqual(spans["search"].attributes["count"], 3)
        self.assertNotIn("skipped", spans["search"].attributes)

    def test_unexpected_exception_is_an_error(self):
        with recorded() as exporter, request_span():
            with self.assertRaises(RuntimeError), tracing.span("lcp.license.issue"):
                raise RuntimeError("LCP server down")
        span = _by_name(exporter)["lcp.license.issue"]
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes["error.type"], "RuntimeError")
        self.assertEqual(span.events[0].name, "exception")

    def test_expected_exception_is_noted_not_an_error(self):
        with recorded() as exporter, request_span():
            with self.assertRaises(KeyError), tracing.span("step", expected=(KeyError,)):
                raise KeyError("x")
        span = _by_name(exporter)["step"]
        self.assertEqual(span.status.status_code, StatusCode.UNSET)
        self.assertEqual(span.attributes["error.type"], "KeyError")

    def test_client_error_problem_detail_is_not_an_error(self):
        with recorded() as exporter, request_span():
            with self.assertRaises(ProblemDetailException), tracing.span("auth.authenticate"):
                raise ProblemDetailException("Invalid token.", status=HTTPStatus.UNAUTHORIZED)
        self.assertEqual(_by_name(exporter)["auth.authenticate"].status.status_code, StatusCode.UNSET)

    def test_server_error_problem_detail_is_an_error(self):
        with recorded() as exporter, request_span():
            with self.assertRaises(ProblemDetailException), tracing.span("step"):
                raise ProblemDetailException("Boom", status=HTTPStatus.BAD_GATEWAY)
        self.assertEqual(_by_name(exporter)["step"].status.status_code, StatusCode.ERROR)

    def test_traced_derives_attributes_from_the_call(self):
        @tracing.traced("lcp.license.return", lambda license_id, **kwargs: {tracing.LICENSE_ID: license_id})
        def give_back(license_id, reason=None):
            return reason

        with recorded() as exporter, request_span():
            self.assertEqual(give_back("abc", reason="done"), "done")
        self.assertEqual(_by_name(exporter)["lcp.license.return"].attributes[tracing.LICENSE_ID], "abc")

    def test_traced_is_transparent_without_a_trace(self):
        calls = []

        @tracing.traced("x", lambda: calls.append("attributes") or {})
        def work():
            return 42

        with recorded() as exporter:
            self.assertEqual(work(), 42)
        self.assertEqual(calls, [])
        self.assertEqual(exporter.get_finished_spans(), ())


class RootSpanTests(SimpleTestCase):
    def test_item_is_a_new_trace_linked_to_the_batch(self):
        with recorded() as exporter:
            with tracing.span("command backfill_extraction", require_parent=False) as batch:
                with tracing.root_span("extraction.backfill.item", {tracing.ACQUISITION_ID: "a1"}):
                    # What the item causes (a Celery PRODUCER span) belongs to the item's trace.
                    with tracing.span("apply_async/evilflowers_analyzer.extract", kind=SpanKind.PRODUCER):
                        pass
                # Back in the batch afterwards.
                self.assertEqual(trace.get_current_span(), batch)

        spans = _by_name(exporter)
        batch, item = spans["command backfill_extraction"], spans["extraction.backfill.item"]
        producer = spans["apply_async/evilflowers_analyzer.extract"]
        self.assertIsNone(item.parent)
        self.assertNotEqual(item.context.trace_id, batch.context.trace_id)
        self.assertEqual(producer.context.trace_id, item.context.trace_id)
        self.assertEqual(producer.parent.span_id, item.context.span_id)
        self.assertEqual(item.links[0].context.span_id, batch.context.span_id)
        self.assertEqual(item.links[0].attributes["evilflowers.link.kind"], "batch")
        # The batch → items direction is `{ link:traceID = ... }`, not events (capped at 128 per span).
        self.assertEqual(batch.events, ())

    def test_every_item_of_a_large_batch_keeps_its_link(self):
        with recorded() as exporter:
            with tracing.span("command backfill_extraction", require_parent=False) as batch:
                for _ in range(200):
                    with tracing.root_span("extraction.backfill.item"):
                        pass
        batch_id = batch.get_span_context().trace_id
        items = [s for s in exporter.get_finished_spans() if s.name == "extraction.backfill.item"]
        self.assertEqual(len(items), 200)
        self.assertTrue(all(item.links[0].context.trace_id == batch_id for item in items))
        self.assertEqual(len({item.context.trace_id for item in items}), 200)

    def test_no_op_without_a_batch_span(self):
        with recorded() as exporter:
            with tracing.root_span("extraction.backfill.item") as item:
                self.assertFalse(item.is_recording())
        self.assertEqual(exporter.get_finished_spans(), ())


class RouteAttributeTests(SimpleTestCase):
    def test_known_ids_only_and_never_a_person(self):
        run_id = uuid.uuid4()
        with recorded() as exporter:
            with request_span():
                tracing.TraceContextMiddleware.process_view(
                    None,
                    None,
                    (),
                    {"run_id": run_id, "job": "figures", "catalog_name": "stu", "user_id": uuid.uuid4()},
                )
        attributes = _by_name(exporter)["GET api/v1/test"].attributes
        # Spelled as the extraction services spell them, so one TraceQL query spans every service.
        self.assertEqual(attributes["evilflowers.extraction.run_id"], str(run_id))
        self.assertEqual(attributes["evilflowers.extraction.job"], "figures")
        self.assertEqual(attributes["evilflowers.catalog.url_name"], "stu")
        self.assertFalse(any("user" in key for key in attributes))


class CallerTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")

    def _caller(self, **request_attributes):
        for name, value in request_attributes.items():
            setattr(self.request, name, value)
        with recorded() as exporter:
            with request_span():
                tracing.set_caller(self.request, request_attributes.get("auth_method"))
        return _by_name(exporter)["GET api/v1/test"].attributes

    def test_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        attributes = self._caller(user=AnonymousUser())
        self.assertEqual(attributes[tracing.AUTH_METHOD], "none")
        self.assertNotIn(tracing.ENDUSER_ID, attributes)
        self.assertNotIn(tracing.AUTH_SUPERUSER, attributes)

    def test_service_api_key_without_the_reader(self):
        user = mock.Mock(is_authenticated=True, is_superuser=False, pk=uuid.uuid4())
        key = mock.Mock(pk=uuid.uuid4())
        attributes = self._caller(user=user, api_key=key, auth_method="bearer", auth_token_type="api_key")
        self.assertEqual(attributes[tracing.API_KEY_ID], str(key.pk))
        self.assertEqual(attributes[tracing.AUTH_TOKEN_TYPE], "api_key")
        self.assertNotIn(tracing.ENDUSER_ID, attributes)

    @override_settings(EVILFLOWERS_TRACE_ENDUSER=True)
    def test_reader_only_when_enabled(self):
        user = mock.Mock(is_authenticated=True, is_superuser=True, pk=uuid.uuid4())
        attributes = self._caller(user=user, auth_method="basic")
        self.assertEqual(attributes[tracing.ENDUSER_ID], str(user.pk))
        self.assertTrue(attributes[tracing.AUTH_SUPERUSER])


class LdapClientSpanTests(SimpleTestCase):
    CONFIG = {"URI": "ldaps://ldap.example.org", "ROOT_DN": "dc=x", "FILTER": "(uid={username})"}

    def test_refused_password_is_not_a_directory_fault(self):
        from apps.core.services.ldap_directory import ldap_bind

        connection = mock.Mock()
        connection.simple_bind_s.side_effect = ldap.INVALID_CREDENTIALS({"desc": "Invalid credentials"})
        with recorded() as exporter, request_span():
            with self.assertRaises(ldap.INVALID_CREDENTIALS):
                ldap_bind(connection, self.CONFIG, "uid=x,dc=x", "wrong", mode="user")
        span = _by_name(exporter)["ldap bind"]
        self.assertEqual(span.kind, SpanKind.CLIENT)
        self.assertEqual(span.attributes["server.address"], "ldap.example.org")
        self.assertEqual(span.attributes["server.port"], 636)
        self.assertEqual(span.attributes["evilflowers.ldap.bind_mode"], "user")
        self.assertEqual(span.status.status_code, StatusCode.UNSET)
        # The DN and the password never reach the span.
        self.assertNotIn("uid=x,dc=x", repr(dict(span.attributes)))

    def test_search_records_whether_the_user_was_found(self):
        from apps.core.services.ldap_directory import search_profile

        connection = mock.Mock()
        connection.result.return_value = (None, [("uid=x,dc=x", {"mail": [b"x@example.org"]})])
        with recorded() as exporter, request_span():
            self.assertEqual(search_profile(connection, self.CONFIG, "x", ["mail"]), {"mail": [b"x@example.org"]})
        span = _by_name(exporter)["ldap search"]
        self.assertTrue(span.attributes["evilflowers.ldap.found"])
        self.assertNotIn("x@example.org", repr(dict(span.attributes)))

    def _sync(self, role):
        from apps.core.models import AuthSource
        from apps.core.services import ldap_directory

        connection = mock.Mock()
        connection.result.return_value = (None, [])
        source = mock.Mock(
            pk=uuid.uuid4(), driver=AuthSource.Driver.LDAP, content={**self.CONFIG, "USER_ATTR_MAP": {"name": "cn"}}
        )
        source.name = "example"
        users = [mock.Mock(username="a"), mock.Mock(username="b")]
        with recorded() as exporter:
            with (
                mock.patch.object(ldap_directory, "ldap_initialize", return_value=connection),
                mock.patch("evil_flowers_catalog.telemetry.current_role", return_value=role),
            ):
                with tracing.span("command sync_ldap_profiles", require_parent=False):
                    ldap_directory.sync_users(source, users)
        spans = exporter.get_finished_spans()
        return next(s for s in spans if s.name.startswith("command")), [s for s in spans if s.name == "ldap.sync.user"]

    def test_sync_under_a_command_is_a_linked_trace_per_user(self):
        command, per_user = self._sync("command")
        self.assertEqual(len(per_user), 2)
        for span in per_user:
            self.assertIsNone(span.parent)
            self.assertEqual(span.links[0].context.span_id, command.context.span_id)

    def test_sync_inside_a_request_stays_in_the_request_trace(self):
        command, per_user = self._sync("gunicorn")
        self.assertEqual(len(per_user), 2)
        for span in per_user:
            self.assertEqual(span.parent.span_id, command.context.span_id)


class SmtpClientSpanTests(SimpleTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="relay.example.org")
    def test_smtp_relay_is_a_service_graph_node(self):
        from apps.notifications.services import _deliver

        email = mock.Mock(attachments=[object()])
        with recorded() as exporter, request_span():
            _deliver(email)
        span = _by_name(exporter)["smtp send"]
        email.send.assert_called_once_with()
        self.assertEqual(span.kind, SpanKind.CLIENT)
        self.assertEqual(span.attributes["server.address"], "relay.example.org")
        self.assertEqual(span.attributes["evilflowers.email.backend"], "smtp")
        self.assertEqual(span.attributes["evilflowers.email.attachments"], 1)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_local_backend_has_no_phantom_relay(self):
        from apps.notifications.services import _deliver

        with recorded() as exporter, request_span():
            _deliver(mock.Mock(attachments=[]))
        self.assertNotIn("server.address", _by_name(exporter)["smtp send"].attributes)


class SearchSpanTests(SimpleTestCase):
    def test_channels_are_children_of_the_search_and_the_text_is_never_recorded(self):
        from apps.core.models import Entry
        from apps.core.search.query import Channel, SearchState

        state = SearchState("secret words", channels=(Channel.LEXICAL,))
        ids = [uuid.uuid4(), uuid.uuid4()]
        with recorded() as exporter, request_span():
            with mock.patch.object(SearchState, "lexical_ranking", return_value=ids):
                state.apply(Entry.objects.none())
        spans = _by_name(exporter)
        search, lexical = spans["search"], spans["search.lexical"]
        self.assertEqual(lexical.parent.span_id, search.context.span_id)
        self.assertEqual(lexical.attributes["evilflowers.search.hits"], 2)
        self.assertEqual(search.attributes["evilflowers.search.results"], 2)
        self.assertEqual(search.attributes["evilflowers.search.words"], 2)
        self.assertEqual(search.attributes["evilflowers.search.level"], "strict")
        for span in spans.values():
            self.assertNotIn("secret", repr(dict(span.attributes)))

    def test_facet_pass_on_a_memo_hit_adds_no_span(self):
        from apps.core.models import Entry
        from apps.core.search.query import Channel, SearchState

        state = SearchState("words", channels=(Channel.LEXICAL,))
        with recorded() as exporter, request_span():
            with mock.patch.object(SearchState, "lexical_ranking", return_value=[uuid.uuid4()]) as ranking:
                with mock.patch.object(SearchState, "_ranking_key", return_value=("sql", "()")):
                    state.apply(Entry.objects.none())
                    state.apply(Entry.objects.none(), record=False)
        self.assertEqual(ranking.call_count, 1)
        names = [span.name for span in exporter.get_finished_spans()]
        self.assertEqual(sorted(names), ["GET api/v1/test", "search", "search.lexical"])


class ServerTimingTests(SimpleTestCase):
    @staticmethod
    def _respond(**headers):
        from django.http import HttpResponse

        request = RequestFactory().get("/api/v1/entries", **headers)
        return tracing.TraceContextMiddleware(lambda request: HttpResponse("ok"))(request)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://portal.example.org"], CORS_ALLOW_ALL_ORIGINS=False)
    def test_response_carries_the_server_spans_traceparent(self):
        with recorded() as exporter, request_span():
            response = self._respond(HTTP_ORIGIN="https://portal.example.org")
        server = _by_name(exporter)["GET api/v1/test"]
        expected = f"00-{server.context.trace_id:032x}-{server.context.span_id:016x}-01"
        self.assertEqual(response["Server-Timing"], f'traceparent;desc="{expected}"')
        self.assertEqual(response["Timing-Allow-Origin"], "https://portal.example.org")

    @override_settings(CORS_ALLOWED_ORIGINS=["https://portal.example.org"], CORS_ALLOW_ALL_ORIGINS=False)
    def test_foreign_origin_gets_no_timing_allow_origin(self):
        with recorded(), request_span():
            response = self._respond(HTTP_ORIGIN="https://evil.example.org")
        self.assertIn("Server-Timing", response)
        self.assertNotIn("Timing-Allow-Origin", response)

    def test_nothing_outside_a_trace(self):
        with recorded():
            response = self._respond()
        self.assertNotIn("Server-Timing", response)

    def test_fetch_may_read_it_cross_origin(self):
        from django.conf import settings

        self.assertIn("Server-Timing", settings.CORS_EXPOSE_HEADERS)


class BrowserTraceContextTests(SimpleTestCase):
    @override_settings(CORS_ALLOWED_ORIGINS=["https://portal.example.org"])
    def test_preflight_admits_trace_context(self):
        response = self.client.options(
            "/api/v1/entries",
            HTTP_ORIGIN="https://portal.example.org",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="traceparent,tracestate,authorization",
        )
        allowed = response["Access-Control-Allow-Headers"].lower()
        self.assertIn("traceparent", allowed)
        self.assertIn("tracestate", allowed)
        self.assertIn("authorization", allowed)


class TracedCommandTests(SimpleTestCase):
    def test_call_command_never_sets_up_telemetry(self):
        """Only the CLI path decides a process's telemetry; tests and nested commands stay out of it."""
        with mock.patch("evil_flowers_catalog.telemetry.setup") as setup:
            with mock.patch("apps.core.management.commands.reindex_search.Command.handle", return_value=None):
                call_command("reindex_search")
        setup.assert_not_called()

    def test_cli_run_is_one_root_span_and_flushes(self):
        from apps.core.management.commands.reindex_search import Command

        with recorded() as exporter:
            with (
                mock.patch("evil_flowers_catalog.profiling.setup") as profiling_setup,
                mock.patch("evil_flowers_catalog.telemetry.setup") as setup,
                mock.patch("evil_flowers_catalog.telemetry.exit_on_sigterm"),
                mock.patch("evil_flowers_catalog.telemetry.shutdown") as shutdown,
                mock.patch.object(Command, "handle", return_value=None),
            ):
                Command().run_from_argv(["manage.py", "reindex_search"])
        profiling_setup.assert_called_once_with("command")
        setup.assert_called_once_with("command")
        shutdown.assert_called_once_with()
        span = _by_name(exporter)["command reindex_search"]
        self.assertIsNone(span.parent)
        self.assertEqual(span.attributes["evilflowers.command.name"], "reindex_search")

    def test_failed_command_is_an_error(self):
        from django.core.management import CommandError

        from apps.core.management.commands.reindex_search import Command

        with recorded() as exporter:
            with (
                mock.patch("evil_flowers_catalog.profiling.setup"),
                mock.patch("evil_flowers_catalog.telemetry.setup"),
                mock.patch("evil_flowers_catalog.telemetry.exit_on_sigterm"),
                mock.patch("evil_flowers_catalog.telemetry.shutdown") as shutdown,
                mock.patch.object(Command, "handle", side_effect=CommandError("nope")),
                self.assertRaises(SystemExit),
            ):
                Command(stderr=mock.Mock()).run_from_argv(["manage.py", "reindex_search"])
        shutdown.assert_called_once_with()
        span = _by_name(exporter)["command reindex_search"]
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes["process.exit.code"], 1)


class CommandRoleTests(SimpleTestCase):
    def setUp(self):
        from evil_flowers_catalog import telemetry

        self._saved = dict(telemetry._state)
        telemetry._state.update(role=None, enabled=False)

    def tearDown(self):
        from evil_flowers_catalog import telemetry

        telemetry._state.clear()
        telemetry._state.update(self._saved)

    def test_command_role_exports_traces_but_no_metrics(self):
        from evil_flowers_catalog import telemetry

        with (
            mock.patch.dict("os.environ", {"OTEL_TRACES_EXPORTER": "console", "OTEL_METRICS_EXPORTER": "console"}),
            mock.patch.object(telemetry, "discover_endpoint", return_value=None),
            mock.patch.object(telemetry, "_configure") as configure,
        ):
            self.assertTrue(telemetry.setup("command"))
        configure.assert_called_once_with("command", ["console"], [])

"""IP-019: telemetry setup, collector discovery and JSON logs.

`SimpleTestCase` throughout: nothing here touches the database.
"""

import json
import logging
import os
import socket
import sys
from unittest import mock

from django.test import SimpleTestCase
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from evil_flowers_catalog import telemetry
from evil_flowers_catalog.log_format import JsonFormatter, TextFormatter

# Captured inside a container on the `elvira` bridge network (gateway 172.28.0.1).
ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00001CAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    "eth0\t00000000\t01001CAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
)

TRACE_ID = 0x4BF92F3577B34DA6A3CE929D0E0E4736
SPAN_ID = 0x00F067AA0BA902B7


def _record(msg="hello %s", args=("world",), levelno=logging.INFO, exc_info=None, **extra):
    record = logging.LogRecord("apps.test", levelno, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _active_span():
    return trace.use_span(
        NonRecordingSpan(SpanContext(TRACE_ID, SPAN_ID, is_remote=False, trace_flags=TraceFlags(1))),
        end_on_exit=False,
    )


class JsonFormatterTests(SimpleTestCase):
    def test_one_object_per_line_with_alloy_keys(self):
        line = JsonFormatter().format(_record(msg="two\nlines", args=()))

        self.assertNotIn("\n", line)
        payload = json.loads(line)
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "apps.test")
        self.assertEqual(payload["msg"], "two\nlines")
        self.assertRegex(payload["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertNotIn("trace_id", payload)

    def test_trace_ids_from_the_current_span(self):
        with _active_span():
            payload = json.loads(JsonFormatter().format(_record()))

        self.assertEqual(payload["trace_id"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(payload["span_id"], "00f067aa0ba902b7")

    def test_extra_fields_are_emitted_but_never_override_reserved_keys(self):
        payload = json.loads(JsonFormatter().format(_record(license_id="abc", level="DEBUG", event="readium.x")))

        self.assertEqual(payload["license_id"], "abc")
        self.assertEqual(payload["event"], "readium.x")
        self.assertEqual(payload["level"], "INFO")

    def test_traceback_goes_to_exc(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(levelno=logging.ERROR, exc_info=sys.exc_info())

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["level"], "ERROR")
        self.assertIn("ValueError: boom", payload["exc"])

    def test_values_json_cannot_encode_are_stringified(self):
        payload = json.loads(JsonFormatter().format(_record(when=object())))

        self.assertIsInstance(payload["when"], str)

    def test_text_formatter_appends_trace_id(self):
        with _active_span():
            line = TextFormatter().format(_record())

        self.assertIn("[apps.test] hello world", line)
        self.assertTrue(line.endswith("trace_id=4bf92f3577b34da6a3ce929d0e0e4736"))


class GatewayParserTests(SimpleTestCase):
    def test_default_route(self):
        self.assertEqual(telemetry.parse_default_gateway(ROUTE_TABLE), "172.28.0.1")

    def test_no_default_route(self):
        table = ROUTE_TABLE.splitlines()[0] + "\n" + ROUTE_TABLE.splitlines()[1] + "\n"
        self.assertIsNone(telemetry.parse_default_gateway(table))

    def test_garbage_is_ignored(self):
        self.assertIsNone(telemetry.parse_default_gateway("header\nnot a route table\neth0 00000000 zz 0003\n"))

    def test_missing_route_file(self):
        self.assertIsNone(telemetry.default_gateway("/nonexistent/route"))


class ProbeTests(SimpleTestCase):
    def test_success(self):
        with mock.patch.object(telemetry.socket, "create_connection") as connect:
            self.assertTrue(telemetry.probe("172.28.0.1"))

        connect.assert_called_once_with(("172.28.0.1", 4318), timeout=0.3)

    def test_timeout(self):
        with mock.patch.object(telemetry.socket, "create_connection", side_effect=socket.timeout()):
            self.assertFalse(telemetry.probe("172.28.0.1"))

    def test_refused(self):
        with mock.patch.object(telemetry.socket, "create_connection", side_effect=ConnectionRefusedError()):
            self.assertFalse(telemetry.probe("172.28.0.1"))


@mock.patch.dict(os.environ, {}, clear=True)
class DiscoveryTests(SimpleTestCase):
    def test_explicit_endpoint_wins_without_probing(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318"
        with mock.patch.object(telemetry, "probe") as probe:
            self.assertEqual(telemetry.discover_endpoint(), "http://collector:4318")
        probe.assert_not_called()

    def test_gateway_discovered_and_exported(self):
        with (
            mock.patch.object(telemetry, "default_gateway", return_value="172.28.0.1"),
            mock.patch.object(telemetry, "probe", return_value=True),
            self.assertLogs("evil_flowers_catalog.telemetry", "INFO") as logs,
        ):
            endpoint = telemetry.discover_endpoint()

        self.assertEqual(endpoint, "http://172.28.0.1:4318")
        self.assertEqual(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://172.28.0.1:4318")
        self.assertEqual(os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"], "http/protobuf")
        self.assertIn("discovered collector at http://172.28.0.1:4318", logs.output[0])

    def test_host_ip_takes_precedence_over_gateway(self):
        os.environ["HOST_IP"] = "10.0.0.7"
        with (
            mock.patch.object(telemetry, "default_gateway", return_value="10.244.0.1") as gateway,
            mock.patch.object(telemetry, "probe", return_value=True) as probe,
        ):
            self.assertEqual(telemetry.discover_endpoint(), "http://10.0.0.7:4318")
        probe.assert_called_once_with("10.0.0.7")
        gateway.assert_not_called()

    def test_failure_stays_off_and_logs_once(self):
        with (
            mock.patch.object(telemetry, "default_gateway", return_value="172.28.0.1"),
            mock.patch.object(telemetry, "probe", return_value=False) as probe,
            self.assertLogs("evil_flowers_catalog.telemetry", "INFO") as logs,
        ):
            self.assertIsNone(telemetry.discover_endpoint())
            self.assertIsNone(telemetry.discover_endpoint())

        probe.assert_called_once()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no collector answered", logs.output[0])
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", os.environ)

    def test_autodiscover_false_never_probes(self):
        os.environ["OTEL_EXPORTER_OTLP_AUTODISCOVER"] = "false"
        with mock.patch.object(telemetry, "probe") as probe:
            self.assertIsNone(telemetry.discover_endpoint())
        probe.assert_not_called()


@mock.patch.dict(os.environ, {}, clear=True)
class SetupTests(SimpleTestCase):
    def setUp(self):
        self._saved = dict(telemetry._state)
        telemetry._state.update(role=None, enabled=False)

    def tearDown(self):
        telemetry._state.clear()
        telemetry._state.update(self._saved)

    def test_off_when_sdk_disabled(self):
        os.environ["OTEL_SDK_DISABLED"] = "true"
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318"
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertFalse(telemetry.setup("gunicorn"))
        configure.assert_not_called()

    def test_off_without_endpoint(self):
        os.environ["OTEL_EXPORTER_OTLP_AUTODISCOVER"] = "false"
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertFalse(telemetry.setup("gunicorn"))
        configure.assert_not_called()
        self.assertFalse(telemetry.is_enabled())

    def test_off_when_both_exporters_are_none(self):
        os.environ.update(
            OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318",
            OTEL_TRACES_EXPORTER="none",
            OTEL_METRICS_EXPORTER="none",
        )
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertFalse(telemetry.setup("gunicorn"))
        configure.assert_not_called()

    def test_console_exporters_work_without_endpoint(self):
        os.environ.update(
            OTEL_EXPORTER_OTLP_AUTODISCOVER="false", OTEL_TRACES_EXPORTER="console", OTEL_METRICS_EXPORTER="console"
        )
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertTrue(telemetry.setup("celery-worker"))
        configure.assert_called_once_with("celery-worker", ["console"], ["console"])

    def test_otlp_dropped_without_endpoint(self):
        os.environ.update(OTEL_EXPORTER_OTLP_AUTODISCOVER="false", OTEL_TRACES_EXPORTER="otlp,console")
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertTrue(telemetry.setup("gunicorn"))
        configure.assert_called_once_with("gunicorn", ["console"], [])

    def test_idempotent(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318"
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertTrue(telemetry.setup("gunicorn"))
            self.assertTrue(telemetry.setup("celery-beat"))
        configure.assert_called_once()
        self.assertEqual(telemetry.current_role(), "gunicorn")

    def test_first_decision_sticks_when_off(self):
        os.environ["OTEL_EXPORTER_OTLP_AUTODISCOVER"] = "false"
        self.assertFalse(telemetry.setup("gunicorn"))
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318"
        with mock.patch.object(telemetry, "_configure") as configure:
            self.assertFalse(telemetry.setup("gunicorn"))
        configure.assert_not_called()


@mock.patch.dict(os.environ, {}, clear=True)
class ResourceTests(SimpleTestCase):
    def test_defaults(self):
        attributes = telemetry.build_resource("celery-beat").attributes

        self.assertEqual(attributes["service.name"], "evilflowers")
        self.assertEqual(attributes["service.instance.id"], f"{socket.gethostname()}-{os.getpid()}")
        self.assertEqual(attributes["evilflowers.process.role"], "celery-beat")
        self.assertIn("process.pid", attributes)
        self.assertIn("os.type", attributes)
        self.assertIn("host.name", attributes)

    def test_environment_wins_over_code(self):
        os.environ["OTEL_SERVICE_NAME"] = "evilflowers-dev"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
            "service.namespace=elvira,deployment.environment.name=production,service.version=override"
        )

        attributes = telemetry.build_resource("gunicorn").attributes

        self.assertEqual(attributes["service.name"], "evilflowers-dev")
        self.assertEqual(attributes["service.version"], "override")
        self.assertEqual(attributes["service.namespace"], "elvira")
        self.assertEqual(attributes["deployment.environment.name"], "production")


class SuppressExcludedUrlsMiddlewareTests(SimpleTestCase):
    """The healthcheck's queries must not become root traces of their own."""

    @mock.patch.dict(os.environ, {"OTEL_PYTHON_DJANGO_EXCLUDED_URLS": "api/v1/status"})
    def test_excluded_url_runs_with_instrumentation_suppressed(self):
        from django.test import RequestFactory
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled

        seen = {}

        def view(request):
            seen[request.path] = is_instrumentation_enabled()
            return "response"

        middleware = telemetry.SuppressExcludedUrlsMiddleware(view)
        middleware(RequestFactory().get("/api/v1/status"))
        middleware(RequestFactory().get("/api/v1/entries"))

        self.assertEqual(seen, {"/api/v1/status": False, "/api/v1/entries": True})


class ExceptionMiddlewareSpanStatusTests(SimpleTestCase):
    """IP-019 Q4: handled 4xx is recorded but leaves the server span UNSET; 5xx is ERROR."""

    def _span_after(self, exception):
        from django.test import RequestFactory
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from apps.api.middleware.exceptions import ExceptionMiddleware

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        span = provider.get_tracer("test").start_span("GET /x", kind=trace.SpanKind.SERVER)
        with trace.use_span(span, end_on_exit=True):
            ExceptionMiddleware.process_exception(RequestFactory().get("/x"), exception)
        (finished,) = exporter.get_finished_spans()
        return finished

    def test_handled_4xx_is_recorded_but_not_an_error(self):
        from apps.core.errors import ProblemDetailException

        span = self._span_after(ProblemDetailException("Not found", status=404))

        self.assertEqual(span.status.status_code, trace.StatusCode.UNSET)
        self.assertEqual([event.name for event in span.events], ["exception"])

    def test_handled_5xx_is_an_error(self):
        from apps.core.errors import ProblemDetailException

        span = self._span_after(ProblemDetailException("Upstream failed", status=502))

        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)

    def test_unhandled_exception_is_an_error(self):
        span = self._span_after(ValueError("boom"))

        self.assertEqual(span.status.status_code, trace.StatusCode.ERROR)
        self.assertEqual([event.name for event in span.events], ["exception"])


class McpTelemetryNamesTests(SimpleTestCase):
    """Method and tool names come from the client; only known ones become attributes."""

    def setUp(self):
        from apps.mcp.views import server

        self.server = server

    def test_tool_call(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_entries"}}
        self.assertEqual(self.server._telemetry_names(message), ("tools/call", "search_entries"))

    def test_unknown_tool_and_method_are_bounded(self):
        unknown_tool = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x" * 50}}
        self.assertEqual(self.server._telemetry_names(unknown_tool), ("tools/call", "_OTHER"))
        self.assertEqual(self.server._telemetry_names({"method": "made/up"}), ("_OTHER", None))
        self.assertEqual(self.server._telemetry_names(["not", "a", "message"]), ("_OTHER", None))

    def test_error_type(self):
        self.assertEqual(self.server._error_type({"error": {"code": -32601}}), "-32601")
        self.assertEqual(self.server._error_type({"result": {"isError": True}}), "tool_error")
        self.assertIsNone(self.server._error_type({"result": {"isError": False}}))
        self.assertIsNone(self.server._error_type(None))


class ContextBoundIteratorTests(SimpleTestCase):
    def test_iterates_under_the_creating_context_only(self):
        seen = []

        def body():
            for _ in range(2):
                seen.append(trace.get_current_span().get_span_context().trace_id)
                yield "frame"

        with _active_span():
            iterator = telemetry.ContextBoundIterator(body())

        # Consumed after the request context is gone, as a streaming body is.
        self.assertEqual(list(iterator), ["frame", "frame"])
        self.assertEqual(seen, [TRACE_ID, TRACE_ID])
        self.assertFalse(trace.get_current_span().get_span_context().is_valid)

    def test_close_reaches_the_generator(self):
        closed = []

        def body():
            try:
                yield 1
                yield 2
            finally:
                closed.append(True)

        iterator = telemetry.ContextBoundIterator(body())
        next(iterator)
        iterator.close()

        self.assertEqual(closed, [True])

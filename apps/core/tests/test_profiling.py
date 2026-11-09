"""#111: the Pyroscope SDK starts only when configured, and never starts a real agent here.

`pyroscope.configure` is mocked throughout: a real agent would start a native
sampler thread inside the test runner.
"""

import os
from unittest import mock

from django.test import SimpleTestCase

from evil_flowers_catalog import profiling, telemetry

ENVIRONMENT = {
    "PYROSCOPE_SERVER_ADDRESS": "http://pyroscope.observability.svc.cluster.local:4040",
    "PYROSCOPE_APPLICATION_NAME": "evilflowers",
    "PYROSCOPE_TAGS": "namespace=evilflowers-dev,pod=evilflowers-0,container=evilflowers,cluster=k8s-stu",
}


class _ProfilingStateMixin:
    def setUp(self):
        self._saved = dict(profiling._state)
        profiling._state.update(role=None, enabled=False)

    def tearDown(self):
        profiling._state.clear()
        profiling._state.update(self._saved)


class TagTests(SimpleTestCase):
    def test_parses_key_value_pairs(self):
        self.assertEqual(profiling.parse_tags("a=1, b = 2,c=x=y"), {"a": "1", "b": "2", "c": "x=y"})

    def test_skips_malformed_items(self):
        self.assertEqual(profiling.parse_tags(",novalue,=orphan,ok=1,"), {"ok": "1"})
        self.assertEqual(profiling.parse_tags(""), {})

    def test_role_is_added_but_never_overrides(self):
        with mock.patch.dict(os.environ, {"PYROSCOPE_TAGS": "pod=p"}):
            self.assertEqual(profiling.build_tags("gunicorn"), {"pod": "p", "role": "gunicorn"})
        with mock.patch.dict(os.environ, {"PYROSCOPE_TAGS": "role=custom"}):
            self.assertEqual(profiling.build_tags("gunicorn"), {"role": "custom"})


class ApplicationNameTests(SimpleTestCase):
    def test_precedence(self):
        with mock.patch.dict(os.environ, {"PYROSCOPE_APPLICATION_NAME": "a", "OTEL_SERVICE_NAME": "b"}):
            self.assertEqual(profiling.application_name(), "a")
        with mock.patch.dict(os.environ, {"PYROSCOPE_APPLICATION_NAME": "", "OTEL_SERVICE_NAME": "b"}):
            self.assertEqual(profiling.application_name(), "b")
        with mock.patch.dict(os.environ, {"PYROSCOPE_APPLICATION_NAME": "", "OTEL_SERVICE_NAME": ""}):
            self.assertEqual(profiling.application_name(), "evilflowers")


class SetupTests(_ProfilingStateMixin, SimpleTestCase):
    def test_off_without_server_address(self):
        with (
            mock.patch.dict(os.environ, {"PYROSCOPE_SERVER_ADDRESS": " "}),
            mock.patch("pyroscope.configure") as configure,
        ):
            self.assertFalse(profiling.setup("gunicorn"))
        configure.assert_not_called()
        self.assertFalse(profiling.is_enabled())

    def test_configures_the_agent(self):
        with (
            mock.patch.dict(os.environ, ENVIRONMENT),
            mock.patch("pyroscope.configure") as configure,
            mock.patch("atexit.register"),
        ):
            self.assertTrue(profiling.setup("celery-worker"))

        configure.assert_called_once()
        kwargs = configure.call_args.kwargs
        self.assertEqual(kwargs["application_name"], "evilflowers")
        self.assertEqual(kwargs["server_address"], ENVIRONMENT["PYROSCOPE_SERVER_ADDRESS"])
        self.assertEqual(kwargs["sample_rate"], 100)
        self.assertTrue(kwargs["oncpu"])
        self.assertFalse(kwargs["gil_only"])
        self.assertEqual(kwargs["tags"]["namespace"], "evilflowers-dev")
        self.assertEqual(kwargs["tags"]["role"], "celery-worker")
        self.assertTrue(profiling.is_enabled())

    def test_first_call_decides(self):
        with (
            mock.patch.dict(os.environ, ENVIRONMENT),
            mock.patch("pyroscope.configure") as configure,
            mock.patch("atexit.register"),
        ):
            self.assertTrue(profiling.setup("celery-worker"))
            self.assertTrue(profiling.setup("celery-beat"))
        configure.assert_called_once()

    def test_a_failing_agent_does_not_raise(self):
        with (
            mock.patch.dict(os.environ, ENVIRONMENT),
            mock.patch("pyroscope.configure", side_effect=RuntimeError("boom")),
            self.assertLogs("evil_flowers_catalog.profiling", "WARNING"),
        ):
            self.assertFalse(profiling.setup("gunicorn"))
        self.assertFalse(profiling.is_enabled())

    def test_shutdown_only_when_running(self):
        with mock.patch("pyroscope.shutdown") as shutdown:
            profiling.shutdown()
            shutdown.assert_not_called()

            profiling._state["enabled"] = True
            profiling.shutdown()
            profiling.shutdown()
            shutdown.assert_called_once()


class SpanProcessorTests(_ProfilingStateMixin, SimpleTestCase):
    def test_none_when_profiling_is_off(self):
        self.assertIsNone(profiling.span_processor())

    def test_processor_when_profiling_is_on(self):
        from pyroscope.otel import PyroscopeSpanProcessor

        profiling._state["enabled"] = True
        with mock.patch.object(profiling, "_gevent_patched", return_value=False):
            self.assertIsInstance(profiling.span_processor(), PyroscopeSpanProcessor)

    def test_none_under_gevent(self):
        profiling._state["enabled"] = True
        with mock.patch.object(profiling, "_gevent_patched", return_value=True):
            self.assertIsNone(profiling.span_processor())

    def test_telemetry_adds_it_to_the_tracer_provider(self):
        provider = mock.Mock()
        processor = object()
        with mock.patch.object(profiling, "span_processor", return_value=processor):
            telemetry._link_profiles(provider)
        provider.add_span_processor.assert_called_once_with(processor)

        provider.reset_mock()
        with mock.patch.object(profiling, "span_processor", return_value=None):
            telemetry._link_profiles(provider)
        provider.add_span_processor.assert_not_called()

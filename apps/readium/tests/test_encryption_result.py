"""
Worker result reports and dispatch tracking for encryption jobs (issue #99).

lcpencrypt calls the notify webhook only on success, so before #99 a job that
failed in the worker stayed PENDING forever. The worker now POSTs the outcome
of every task to `/readium/v1/hooks/encryption/result`. Pinned down here:

- a failure report turns the job FAILED with the worker's error,
- a success report is stored but never touches the status (notify owns it),
- a report about an older task (the job was re-queued since) is ignored,
- the dispatch records the task id it sent and the trace it ran in, and both
  webhooks put `lcp.content_id` on their span.
"""

import json
import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.events.executors.celery_executor import CeleryExecurtor
from apps.readium.models import EncryptedContent
from apps.readium.services.content_encryption_service import ContentEncryptionService
from apps.readium.tests.test_encryption_notify import _basic

RESULT_PATH = "/readium/v1/hooks/encryption/result"
NOTIFY_PATH = "/readium/v1/hooks/encryption"
REPORT_URL = "http://worker:s3cr%40t@catalog:8000/readium/v1/hooks/encryption/result"
BROKER = "apps.readium.services.content_encryption_service.get_event_broker"
PREPARE_SOURCE = "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source"


@override_settings(
    EVILFLOWERS_READIUM_BASE_URL="https://efc.example.org",
    EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL=None,
    EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL=None,
)
class EncryptionResultTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="owner", name="Owner", surname="T", auth_source=auth_source)
        cls.user.set_unusable_password()
        cls.user.save()
        catalog = Catalog.objects.create(creator=cls.user, title="Catalog", url_name="catalog")
        cls.entry = Entry.objects.create(creator=cls.user, catalog=catalog, title="South-North Migration")

    def setUp(self):
        acquisition = Acquisition.objects.create(
            entry=self.entry, mime=Acquisition.AcquisitionMIME.PDF, content=f"catalogs/catalog/{uuid.uuid4()}.pdf"
        )
        content_id = str(uuid.uuid4())
        self.job = EncryptedContent.objects.create(
            acquisition=acquisition,
            status=EncryptedContent.EncryptionStatus.PENDING,
            lcp_content_id=content_id,
            encrypted_path=f"{acquisition.upload_base_path()}/encrypted/{content_id}.lcpdf",
        )
        patcher = patch(PREPARE_SOURCE, side_effect=lambda ec: ec.acquisition.content.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def dispatch(self) -> dict:
        """Queue the job; returns the payload handed to the broker."""
        with patch(BROKER) as get_broker:
            ContentEncryptionService._queue_encryption_task(self.job)
        _event, payload = get_broker.return_value.execute.call_args.args
        return payload

    def report(self, success: bool, task_id=None, headers=None, **fields):
        body = {
            "contentid": self.job.lcp_content_id,
            "task_id": task_id if task_id is not None else self.job.task_id,
            "trace_id": "6d98d2e07ce0e553407a00057aad4b39",
            "success": success,
            "error": "",
            **fields,
        }
        return self.client.post(
            RESULT_PATH, data=json.dumps(body), content_type="application/json", headers=headers or {}
        )


class DispatchTrackingTests(EncryptionResultTestCase):
    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL=REPORT_URL)
    def test_dispatch_sends_its_task_id_and_the_report_url(self):
        payload = self.dispatch()

        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.task_id)
        self.assertEqual(payload["task_id"], self.job.task_id)
        self.assertEqual(payload["kwargs"]["report"], REPORT_URL)
        self.assertEqual(self.job.attempts, 1)
        self.assertIsNotNone(self.job.last_dispatched_at)

    def test_report_url_is_none_when_unset(self):
        # The worker sends no report without it, as before #99.
        self.assertIsNone(self.dispatch()["kwargs"]["report"])

    def test_each_dispatch_gets_a_new_task_and_clears_the_last_report(self):
        self.dispatch()
        self.job.refresh_from_db()
        first_task = self.job.task_id
        self.assertEqual(self.report(True, report={"pages": 3}).status_code, 200)

        self.job.refresh_from_db()
        ContentEncryptionService.requeue(self.job)

        self.job.refresh_from_db()
        self.assertNotEqual(self.job.task_id, first_task)
        self.assertEqual(self.job.attempts, 2)
        self.assertIsNone(self.job.report)
        self.assertIsNone(self.job.reported_at)

    def test_dispatch_records_the_trace_and_tags_the_span(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        with provider.get_tracer("test").start_as_current_span("tools/call requeue_encryption") as span:
            self.dispatch()
            expected_trace_id = trace.format_trace_id(span.get_span_context().trace_id)

        self.job.refresh_from_db()
        self.assertEqual(self.job.trace_id, expected_trace_id)
        (finished,) = exporter.get_finished_spans()
        self.assertEqual(finished.attributes["lcp.content_id"], self.job.lcp_content_id)
        self.assertEqual(finished.attributes["lcp.task_id"], self.job.task_id)

    def test_trace_id_is_none_without_telemetry(self):
        self.dispatch()
        self.job.refresh_from_db()
        self.assertIsNone(self.job.trace_id)


class CeleryExecutorTaskIdTests(SimpleTestCase):
    def test_the_minted_task_id_becomes_the_celery_task_id(self):
        # The worker reports `self.request.id`; it must be the id the row stores.
        with patch("apps.events.executors.celery_executor.app") as app:
            CeleryExecurtor().send("task", {"kwargs": {}, "queue": "q", "task_id": "abc"})
        self.assertEqual(app.send_task.call_args.kwargs["task_id"], "abc")


class ResultReportTests(EncryptionResultTestCase):
    def setUp(self):
        super().setUp()
        self.dispatch()
        self.job.refresh_from_db()

    def test_failure_report_marks_the_job_failed_with_the_worker_error(self):
        error = "Encryption failed: … exited with return code 1: open …: no such file or directory"

        response = self.report(False, error=error)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "recorded")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.FAILED)
        self.assertEqual(self.job.error_message, error)
        self.assertIsNotNone(self.job.reported_at)

    def test_withdrawn_package_turns_a_registered_job_failed(self):
        # lcpencrypt withdrew the content after a notify it considered failed
        # (e.g. timed out) although the notify hook had already registered it.
        EncryptedContent.objects.filter(pk=self.job.pk).update(status=EncryptedContent.EncryptionStatus.REGISTERED)

        self.report(False, error="Encryption withdrawn from the License Server: Error notifying the CMS: timeout")

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.FAILED)

    def test_success_report_is_stored_without_touching_the_status(self):
        summary = {"cover": "rendered", "pages": 237, "toc_entries": 136, "warnings": []}

        response = self.report(True, report=summary)

        self.assertEqual(response.status_code, 200)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.PENDING)
        self.assertEqual(self.job.report, summary)
        self.assertIsNotNone(self.job.reported_at)

    def test_success_report_after_notify_keeps_the_job_registered(self):
        self.client.post(
            NOTIFY_PATH, data=json.dumps({"uuid": self.job.lcp_content_id}), content_type="application/json"
        )

        self.report(True, report={"pages": 1})

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.REGISTERED)
        self.assertIsNotNone(self.job.encrypted_at)
        self.assertEqual(self.job.report, {"pages": 1})

    def test_notify_after_success_report_keeps_the_report(self):
        self.report(True, report={"pages": 1})

        self.client.post(
            NOTIFY_PATH, data=json.dumps({"uuid": self.job.lcp_content_id}), content_type="application/json"
        )

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.REGISTERED)
        self.assertEqual(self.job.report, {"pages": 1})

    def test_report_about_an_older_task_is_ignored(self):
        stale_task = self.job.task_id
        ContentEncryptionService.requeue(self.job)

        response = self.report(False, task_id=stale_task, error="old run failed")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.PENDING)
        self.assertIsNone(self.job.error_message)

    def test_job_dispatched_before_tracking_accepts_the_report_and_learns_its_task(self):
        EncryptedContent.objects.filter(pk=self.job.pk).update(task_id=None, trace_id=None)
        task_id = str(uuid.uuid4())

        self.report(True, task_id=task_id, report={"pages": 2})

        self.job.refresh_from_db()
        self.assertEqual(self.job.task_id, task_id)
        self.assertEqual(self.job.trace_id, "6d98d2e07ce0e553407a00057aad4b39")

    def test_unknown_content_is_404(self):
        response = self.client.post(
            RESULT_PATH,
            data=json.dumps({"contentid": str(uuid.uuid4()), "task_id": "t", "success": False}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_malformed_reports_are_400(self):
        for body in ("not json", json.dumps({"success": True}), json.dumps({"contentid": "x"}), json.dumps([1])):
            with self.subTest(body=body):
                response = self.client.post(RESULT_PATH, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400)

    @override_settings(EVILFLOWERS_READIUM_LCPENCRYPT_REPORT_URL=REPORT_URL)
    def test_credentials_from_the_report_url_are_required(self):
        self.assertEqual(self.report(False, error="x").status_code, 401)
        self.assertEqual(
            self.report(False, error="x", headers={"authorization": _basic("worker", "wrong")}).status_code, 401
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, EncryptedContent.EncryptionStatus.PENDING)

        response = self.report(False, error="x", headers={"authorization": _basic("worker", "s3cr@t")})
        self.assertEqual(response.status_code, 200)

    def test_both_webhooks_tag_their_span_with_the_content_id(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("POST /readium/v1/hooks/encryption/result"):
            self.report(True, report={})
        with tracer.start_as_current_span("POST /readium/v1/hooks/encryption"):
            self.client.post(
                NOTIFY_PATH, data=json.dumps({"uuid": self.job.lcp_content_id}), content_type="application/json"
            )

        result_span, notify_span = exporter.get_finished_spans()
        self.assertEqual(result_span.attributes["lcp.content_id"], self.job.lcp_content_id)
        self.assertEqual(result_span.attributes["lcp.task_id"], self.job.task_id)
        self.assertEqual(notify_span.attributes["lcp.content_id"], self.job.lcp_content_id)

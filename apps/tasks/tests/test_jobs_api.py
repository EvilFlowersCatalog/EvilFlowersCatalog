"""IP-031 rev. 2: `/api/v1/jobs` (one declarative resource), OCR reports (#146), the dashboard's LCP
counts (#148) and the listener's outage buffer."""

import json
import shutil
import time
import uuid
from unittest.mock import patch

from django import db
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, Language, User
from apps.files.jobs import queue_ocr
from apps.files.storage import get_storage
from apps.readium.models import EncryptedContent
from apps.readium.services.statistics import ENCRYPTION_STUCK_AFTER
from apps.tasks.application.facade import facade
from apps.tasks.domain.values import JobState, RunStatus
from apps.tasks.management.commands.celery_event_handler import Command
from apps.tasks.models import Job, JobRun
from apps.tasks.tests.test_jobs import FakeDispatcher


class ApiFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="admin", name="A", surname="D", auth_source=auth_source, is_superuser=True
        )
        cls.reader = User.objects.create(username="reader", name="R", surname="D", auth_source=auth_source)
        cls.service = User.objects.create(username="ocr-service", name="O", surname="S", auth_source=auth_source)
        cls.service.user_permissions.add(Permission.objects.get(codename="report_job"))
        language, _ = Language.objects.get_or_create(alpha2="sk", defaults={"name": "Slovak", "alpha3": "slk"})
        if not language.alpha3:
            Language.objects.filter(pk=language.pk).update(alpha3="slk")
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Catalog", url_name="catalog")
        cls.entry = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Sken", language=language)

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        patcher = patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Uploads and OCR outputs land in the real storage root; drop this entry's folder.
        shutil.rmtree(get_storage().path(f"catalogs/{self.catalog.url_name}/{self.entry.pk}"), ignore_errors=True)
        super().tearDown()

    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def send(self, method, path, payload, user):
        return getattr(self.client, method)(
            path, data=json.dumps(payload), content_type="application/json", headers=self.headers(user)
        )

    def acquisition(self):
        acquisition = Acquisition(entry=self.entry, mime=Acquisition.AcquisitionMIME.PDF)
        acquisition.save()
        acquisition.content.save(f"{uuid.uuid4()}.pdf", ContentFile(b"%PDF-1.4 scan"))
        return acquisition


class JobResourceTests(ApiFixtures):
    def test_administrators_only(self):
        self.assertEqual(self.client.get("/api/v1/jobs", headers=self.headers(self.reader)).status_code, 403)
        self.assertEqual(
            self.send("post", "/api/v1/jobs", {"kind": "maintenance.backup"}, self.reader).status_code, 403
        )

    def test_apply_list_detail_and_cancel(self):
        response = self.send("post", "/api/v1/jobs", {"kind": "maintenance.prune_jobs"}, self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        job = response.json()["response"]
        self.assertEqual((job["state"], job["desired_state"], job["generation"]), ("queued", "completed", 1))
        self.assertEqual(job["runs"][0]["status"], "queued")

        listed = self.client.get(
            "/api/v1/jobs?kind=maintenance.prune_jobs&state=queued", headers=self.headers(self.admin)
        )
        self.assertEqual(listed.json()["metadata"]["total"], 1)

        cancelled = self.send("patch", f"/api/v1/jobs/{job['id']}", {"desired_state": "cancelled"}, self.admin)
        self.assertEqual(cancelled.json()["response"]["state"], "cancelled")

    def test_dry_run_and_refusals(self):
        plan = self.send("post", "/api/v1/jobs?dry_run=true", {"kind": "maintenance.backup"}, self.admin)
        self.assertEqual(plan.json()["response"]["outcome"], "would-run")
        self.assertFalse(Job.objects.exists())
        self.assertEqual(self.send("post", "/api/v1/jobs", {"kind": "nope"}, self.admin).status_code, 404)
        bad = self.send("post", "/api/v1/jobs", {"kind": "maintenance.backup", "spec": {"x": 1}}, self.admin)
        self.assertEqual(bad.status_code, 422)

    def test_kinds_summary_and_runs(self):
        kinds = self.client.get("/api/v1/jobs/kinds", headers=self.headers(self.admin)).json()["response"]
        self.assertTrue({"files.ocr", "readium.encrypt", "extraction.run"} <= {kind["kind"] for kind in kinds})
        self.send("post", "/api/v1/jobs", {"kind": "maintenance.backup"}, self.admin)
        summary = self.client.get("/api/v1/jobs/summary", headers=self.headers(self.admin)).json()["response"]
        self.assertIn({"kind": "maintenance.backup", "states": {"queued": 1}, "stale": 0}, summary)
        runs = self.client.get("/api/v1/jobs/runs?kind=maintenance.backup", headers=self.headers(self.admin)).json()
        self.assertEqual(runs["metadata"]["total"], 1)


class DashboardParityTests(ApiFixtures):
    """#148: the jobs the dashboard counts can be listed through the one resource."""

    def test_failed_and_stuck_packages_match_the_statistics(self):
        off = Entry.objects.create(creator=self.admin, catalog=self.catalog, title="LCP off")
        failed = Acquisition.objects.create(
            entry=off, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/x/a.pdf"
        )
        EncryptedContent.objects.create(
            acquisition=failed,
            lcp_content_id=str(uuid.uuid4()),
            encrypted_path="x",
            status=EncryptedContent.EncryptionStatus.FAILED,
            error_message="exit 1",
        )
        stuck = Acquisition.objects.create(entry=off, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/x/b.pdf")
        EncryptedContent.objects.create(
            acquisition=stuck,
            lcp_content_id=str(uuid.uuid4()),
            encrypted_path="y",
            last_dispatched_at=timezone.now() - ENCRYPTION_STUCK_AFTER * 2,
        )
        facade.discover("readium.encrypt")  # the deploy-time sync: rows nobody declared get their job

        def total(query):
            response = self.client.get(f"/api/v1/jobs?kind=readium.encrypt&{query}", headers=self.headers(self.admin))
            return response.json()["metadata"]["total"]

        today = timezone.localdate().isoformat()
        stats = self.client.get(
            f"/api/v1/statistics?catalog_id={self.catalog.pk}&from={today}&to={today}",
            headers=self.headers(self.admin),
        )
        attention = stats.json()["response"]["attention"]
        self.assertEqual(attention["encryption_failed"], total("state=failed"))
        self.assertEqual(attention["encryption_stuck"], total("state=queued&stale=true"))


@override_settings(EVILFLOWERS_OCR_ENABLED=True, EVILFLOWERS_JOBS_REPORT_BASE_URL="http://catalog:8000")
class OcrTests(ApiFixtures):
    def apply(self, acquisition):
        job = facade.apply("files.ocr", acquisition.pk, user=self.admin)
        return job, self.dispatcher.sent[-1]

    def test_dispatch_contract(self):
        acquisition = self.acquisition()
        job, sent = self.apply(acquisition)
        run_id = str(job.current_run_id)
        self.assertEqual((sent["task_id"], sent["queue"]), (run_id, "evilflowers_ocr_worker"))
        kwargs = sent["kwargs"]
        self.assertEqual(kwargs["destination"], f"{acquisition.upload_base_path()}/ocr/{run_id}.pdf")
        self.assertEqual(kwargs["language"], "slk")
        self.assertEqual(kwargs["report_url"], f"http://catalog:8000/api/v1/jobs/runs/{run_id}/report")
        self.assertEqual(kwargs["job_id"], str(job.pk))

    def report(self, run_id, payload, user=None):
        return self.send("post", f"/api/v1/jobs/runs/{run_id}/report", payload, user or self.service)

    def test_report_permission_and_cancelled_jobs(self):
        job, _ = self.apply(self.acquisition())
        self.assertEqual(self.report(job.current_run_id, {"status": "skipped"}, self.reader).status_code, 403)
        facade.patch(job.pk, desired="cancelled", user=self.admin)
        self.assertEqual(self.report(job.current_run_id, {"status": "skipped"}).status_code, 409)

    def test_report_from_a_superseded_run_is_refused(self):
        acquisition = self.acquisition()
        job, _ = self.apply(acquisition)
        old_run = job.current_run_id
        # the first run is lost (e.g. the sweeper gave up on it); a newer generation sends a new run
        JobRun.objects.filter(pk=old_run).update(status="lost")
        facade.run_changed(JobRun.objects.get(pk=old_run))
        job = facade.apply("files.ocr", acquisition.pk, spec={"redo": True}, user=self.admin)
        self.assertNotEqual(job.current_run_id, old_run)
        self.assertEqual(self.report(old_run, {"status": "skipped"}).status_code, 409)
        self.assertEqual(self.report(job.current_run_id, {"status": "skipped"}).status_code, 200)

    def test_done_report_replaces_the_file_and_declares_follow_ups(self):
        acquisition = self.acquisition()
        Entry.objects.filter(pk=self.entry.pk).update(
            config={**self.entry.config, "evilflowers_ocr_rewrite": True, "readium_enabled": True}
        )
        acquisition = Acquisition.objects.get(pk=acquisition.pk)
        original = acquisition.content.name
        job, sent = self.apply(acquisition)
        destination = sent["kwargs"]["destination"]
        get_storage().save(destination, ContentFile(b"%PDF-1.4 ocr"))

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("apps.readium.services.content_encryption_service.get_event_broker"),
            patch(
                "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source",
                side_effect=lambda ec: ec.acquisition.content.name,
            ),
        ):
            response = self.report(job.current_run_id, {"status": "done", "output": destination, "pages": 3})
        self.assertEqual(response.status_code, 200, response.content)
        acquisition.refresh_from_db()
        self.assertEqual((acquisition.content.name, acquisition.checksum_cached), (destination, None))
        self.assertEqual(response.json()["response"]["report"]["original"], original)
        follow_up = Job.objects.get(kind="readium.encrypt", subject_id=acquisition.pk)
        self.assertEqual(follow_up.cause_id, job.pk)

        again = self.report(job.current_run_id, {"status": "failed"})
        self.assertEqual(again.json()["response"]["report"]["applied"], "replaced")  # idempotent

    def test_done_report_adds_a_derived_acquisition_without_rewrite(self):
        acquisition = self.acquisition()
        job, sent = self.apply(acquisition)
        destination = sent["kwargs"]["destination"]
        get_storage().save(destination, ContentFile(b"%PDF-1.4 ocr"))
        report = self.report(job.current_run_id, {"status": "done", "output": destination}).json()["response"][
            "report"
        ]
        self.assertEqual(report["applied"], "derived")
        self.assertEqual(Acquisition.objects.get(pk=report["derived_acquisition_id"]).content.name, destination)

    def test_report_naming_another_file_is_refused(self):
        job, _ = self.apply(self.acquisition())
        response = self.report(job.current_run_id, {"status": "done", "output": "catalogs/other/secret.pdf"})
        self.assertEqual(response.status_code, 400)

    def test_upload_declares_ocr_only_when_the_entry_asks(self):
        acquisition = self.acquisition()
        with self.captureOnCommitCallbacks(execute=True):
            queue_ocr(acquisition)
        self.assertFalse(Job.objects.filter(kind="files.ocr").exists())
        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, "evilflowers_ocr_enabled": True})
        with self.captureOnCommitCallbacks(execute=True):
            queue_ocr(Acquisition.objects.get(pk=acquisition.pk))
        job = Job.objects.get(kind="files.ocr")
        self.assertIsNone(job.requested_by)
        self.assertEqual(job.state, JobState.QUEUED)


class ListenerBufferTests(TestCase):
    def test_events_survive_a_database_outage(self):
        command = Command()
        task_id = str(uuid.uuid4())
        received = {"type": "task-received", "uuid": task_id, "timestamp": time.time(), "name": "x.y"}
        succeeded = {"type": "task-succeeded", "uuid": task_id, "timestamp": time.time() + 1}
        with (
            patch("apps.tasks.infrastructure.ledger.apply_event", side_effect=db.OperationalError("login failing")),
            patch.object(Command, "_reset_connections"),
        ):
            command.handle_event(received)
        self.assertEqual(len(command.buffer), 1)
        command._next_retry = 0
        command.handle_event(succeeded)
        self.assertEqual(len(command.buffer), 0)
        self.assertEqual(JobRun.objects.get(pk=task_id).status, RunStatus.SUCCEEDED)

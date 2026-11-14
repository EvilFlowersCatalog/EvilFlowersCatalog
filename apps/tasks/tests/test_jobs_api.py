"""IP-031: `/api/v1/jobs`, the OCR kind (#146) and the listener's outage buffer."""

import json
import shutil
import time
import uuid
from unittest.mock import patch

from django import db
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, Language, User
from apps.files.jobs import queue_ocr
from apps.files.storage import get_storage
from apps.tasks.management.commands.celery_event_handler import Command
from apps.tasks.models import JobProtocol
from apps.tasks.services import JobService

Status = JobProtocol.JobStatus
SEND = "evil_flowers_catalog.celery.app.send_task"


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

    def tearDown(self):
        # Uploads and OCR outputs land in the real storage root; drop this entry's folder.
        shutil.rmtree(get_storage().path(f"catalogs/{self.catalog.url_name}/{self.entry.pk}"), ignore_errors=True)
        super().tearDown()

    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def post(self, path, payload, user):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json", headers=self.headers(user)
        )

    def acquisition(self):
        acquisition = Acquisition(entry=self.entry, mime=Acquisition.AcquisitionMIME.PDF)
        acquisition.save()
        acquisition.content.save(f"{uuid.uuid4()}.pdf", ContentFile(b"%PDF-1.4 scan"))
        return acquisition


class JobApiTests(ApiFixtures):
    def test_jobs_are_for_administrators_only(self):
        response = self.client.get("/api/v1/jobs", headers=self.headers(self.reader))
        self.assertEqual(response.status_code, 403)

    def test_start_list_detail_and_revoke(self):
        with patch(SEND):
            response = self.post("/api/v1/jobs", {"kind": "maintenance.prune_jobs"}, self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        job = response.json()["response"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["kind"], "maintenance.prune_jobs")

        listed = self.client.get(
            "/api/v1/jobs?kind=maintenance.prune_jobs&status=queued", headers=self.headers(self.admin)
        )
        self.assertEqual(listed.json()["metadata"]["total"], 1)

        detail = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers(self.admin)).json()["response"]
        self.assertEqual([event["type"] for event in detail["events"]], ["queued"])

        with patch("evil_flowers_catalog.celery.app.control.revoke"):
            revoked = self.post(f"/api/v1/jobs/{job['id']}/revoke", {}, self.admin)
        self.assertEqual(revoked.status_code, 202)
        self.assertEqual(revoked.json()["response"]["status"], "revoked")

    def test_conflicts_and_unknown_kinds(self):
        with patch(SEND):
            self.post("/api/v1/jobs", {"kind": "maintenance.prune_jobs"}, self.admin)
            again = self.post("/api/v1/jobs", {"kind": "maintenance.prune_jobs"}, self.admin)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.post("/api/v1/jobs", {"kind": "nope"}, self.admin).status_code, 404)

    def test_kinds_and_summary(self):
        kinds = self.client.get("/api/v1/jobs/kinds", headers=self.headers(self.admin)).json()["response"]
        names = {kind["name"] for kind in kinds}
        self.assertTrue({"files.ocr", "readium.encrypt", "extraction.run"} <= names)
        encrypt = next(kind for kind in kinds if kind["name"] == "readium.encrypt")
        self.assertEqual({action["name"] for action in encrypt["actions"]}, {"requeue", "reencrypt"})

        JobProtocol.objects.create(name="x", kind="files.ocr", status=Status.FAILED)
        summary = self.client.get("/api/v1/jobs/summary", headers=self.headers(self.admin)).json()["response"]
        self.assertIn({"kind": "files.ocr", "statuses": {"failed": 1}, "stale": 0}, summary)

    def test_latest_keeps_one_job_per_subject(self):
        acquisition = self.acquisition()
        older = JobProtocol.objects.create(
            name="t", kind="files.ocr", subject_id=acquisition.pk, entry=self.entry, status=Status.FAILED
        )
        newer = JobProtocol.objects.create(
            name="t", kind="files.ocr", subject_id=acquisition.pk, entry=self.entry, status=Status.SUCCEEDED
        )
        ids = [
            item["id"]
            for item in self.client.get(
                f"/api/v1/jobs?entry_id={self.entry.pk}&latest=true", headers=self.headers(self.admin)
            ).json()["items"]
        ]
        self.assertEqual(ids, [str(newer.pk)])
        self.assertNotIn(str(older.pk), ids)


@override_settings(EVILFLOWERS_OCR_ENABLED=True, EVILFLOWERS_JOBS_REPORT_BASE_URL="http://catalog:8000")
class OcrKindTests(ApiFixtures):
    def start(self, acquisition):
        with patch(SEND) as send:
            job = JobService.start("files.ocr", acquisition.pk, user=self.admin)
        return job, send.call_args.kwargs

    def test_dispatch_contract(self):
        acquisition = self.acquisition()
        job, call = self.start(acquisition)
        kwargs = call["kwargs"]
        self.assertEqual(call["task_id"], str(job.pk))
        self.assertEqual(call["queue"], "evilflowers_ocr_worker")
        self.assertEqual(kwargs["source"], acquisition.content.name)
        self.assertEqual(kwargs["destination"], f"{acquisition.upload_base_path()}/ocr/{job.pk}.pdf")
        self.assertEqual(kwargs["language"], "slk")
        self.assertEqual(kwargs["report_url"], f"http://catalog:8000/api/v1/jobs/{job.pk}/report")

    @override_settings(EVILFLOWERS_OCR_ENABLED=False)
    def test_off_without_a_worker(self):
        response = self.post(
            "/api/v1/jobs", {"kind": "files.ocr", "subject_id": str(self.acquisition().pk)}, self.admin
        )
        self.assertEqual(response.status_code, 422)

    def test_report_needs_the_report_permission(self):
        job, _ = self.start(self.acquisition())
        response = self.post(f"/api/v1/jobs/{job.pk}/report", {"status": "skipped"}, self.admin)
        self.assertEqual(response.status_code, 200)  # superusers hold every permission
        job2, _ = self.start(self.acquisition())
        self.assertEqual(
            self.post(f"/api/v1/jobs/{job2.pk}/report", {"status": "skipped"}, self.reader).status_code, 403
        )

    def test_done_report_replaces_the_file_when_rewrite_is_on(self):
        acquisition = self.acquisition()
        # After the upload: saving an acquisition saves its (cached) entry again.
        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, "evilflowers_ocr_rewrite": True})
        acquisition = Acquisition.objects.get(pk=acquisition.pk)
        original = acquisition.content.name
        Acquisition.objects.filter(pk=acquisition.pk).update(checksum_cached="old")
        job, call = self.start(acquisition)
        destination = call["kwargs"]["destination"]
        get_storage().save(destination, ContentFile(b"%PDF-1.4 ocr"))

        response = self.post(
            f"/api/v1/jobs/{job.pk}/report",
            {"status": "done", "output": destination, "pages": 3, "pages_ocred": 3},
            self.service,
        )
        self.assertEqual(response.status_code, 200, response.content)
        acquisition.refresh_from_db()
        self.assertEqual(acquisition.content.name, destination)
        self.assertIsNone(acquisition.checksum_cached)
        report = response.json()["response"]["report"]
        self.assertEqual(report["applied"], "replaced")
        self.assertEqual(report["original"], original)

        again = self.post(f"/api/v1/jobs/{job.pk}/report", {"status": "failed"}, self.service)
        self.assertEqual(again.json()["response"]["report"]["applied"], "replaced")  # idempotent

    def test_done_report_adds_a_derived_acquisition_when_rewrite_is_off(self):
        acquisition = self.acquisition()
        job, call = self.start(acquisition)
        destination = call["kwargs"]["destination"]
        get_storage().save(destination, ContentFile(b"%PDF-1.4 ocr"))
        response = self.post(f"/api/v1/jobs/{job.pk}/report", {"status": "done", "output": destination}, self.service)
        report = response.json()["response"]["report"]
        self.assertEqual(report["applied"], "derived")
        acquisition.refresh_from_db()
        self.assertNotEqual(acquisition.content.name, destination)
        self.assertEqual(Acquisition.objects.get(pk=report["derived_acquisition_id"]).content.name, destination)

    def test_report_naming_another_file_is_refused(self):
        job, _ = self.start(self.acquisition())
        response = self.post(
            f"/api/v1/jobs/{job.pk}/report", {"status": "done", "output": "catalogs/other/secret.pdf"}, self.service
        )
        self.assertEqual(response.status_code, 400)

    def test_report_for_a_revoked_job_is_refused(self):
        job, _ = self.start(self.acquisition())
        with patch("evil_flowers_catalog.celery.app.control.revoke"):
            JobService.revoke(job, user=self.admin)
        response = self.post(f"/api/v1/jobs/{job.pk}/report", {"status": "skipped"}, self.service)
        self.assertEqual(response.status_code, 409)

    def test_upload_starts_ocr_only_when_the_entry_asks(self):
        acquisition = self.acquisition()
        with patch(SEND), self.captureOnCommitCallbacks(execute=True):
            queue_ocr(acquisition)
        self.assertFalse(JobProtocol.objects.filter(kind="files.ocr").exists())

        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, "evilflowers_ocr_enabled": True})
        acquisition = Acquisition.objects.get(pk=acquisition.pk)
        with patch(SEND), self.captureOnCommitCallbacks(execute=True):
            queue_ocr(acquisition)
        job = JobProtocol.objects.get(kind="files.ocr")
        self.assertIsNone(job.requested_by)


class ListenerBufferTests(TestCase):
    def test_events_survive_a_database_outage(self):
        command = Command()
        task_id = str(uuid.uuid4())
        received = {"type": "task-received", "uuid": task_id, "timestamp": time.time(), "name": "x.y"}
        succeeded = {"type": "task-succeeded", "uuid": task_id, "timestamp": time.time() + 1}

        with (
            patch("apps.tasks.ledger.apply_event", side_effect=db.OperationalError("server login has been failing")),
            patch.object(Command, "_reset_connections"),
        ):
            command.handle_event(received)
        self.assertEqual(len(command.buffer), 1)
        self.assertFalse(JobProtocol.objects.filter(pk=task_id).exists())

        command._next_retry = 0
        command.handle_event(succeeded)
        self.assertEqual(len(command.buffer), 0)
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.SUCCEEDED)

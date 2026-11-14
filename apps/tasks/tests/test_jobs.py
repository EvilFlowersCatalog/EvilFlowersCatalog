"""IP-031: the job ledger, the job service and the housekeeping."""

import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, Language, User
from apps.readium.models import EncryptedContent
from apps.tasks import ledger
from apps.tasks.kinds import JobRefused, registry
from apps.tasks.maintenance import prune_jobs, sweep_lost_jobs
from apps.tasks.models import JobEvent, JobProtocol
from apps.tasks.services import JobService

Status = JobProtocol.JobStatus
SEND = "evil_flowers_catalog.celery.app.send_task"
REVOKE = "evil_flowers_catalog.celery.app.control.revoke"
BROKER = "apps.readium.services.content_encryption_service.get_event_broker"
PREPARE_SOURCE = "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source"


def event(type_, task_id, ts=None, **fields):
    return {"type": type_, "uuid": str(task_id), "timestamp": ts if ts is not None else time.time(), **fields}


class Fixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(
            username="admin", name="A", surname="D", auth_source=auth_source, is_superuser=True
        )
        cls.reader = User.objects.create(username="reader", name="R", surname="D", auth_source=auth_source)
        language, _ = Language.objects.get_or_create(alpha2="sk", defaults={"name": "Slovak", "alpha3": "slk"})
        cls.catalog = Catalog.objects.create(creator=cls.admin, title="Catalog", url_name="catalog")
        cls.entry = Entry.objects.create(creator=cls.admin, catalog=cls.catalog, title="Kniha", language=language)

    def acquisition(self, mime=Acquisition.AcquisitionMIME.PDF):
        return Acquisition.objects.create(entry=self.entry, mime=mime, content=f"catalogs/catalog/{uuid.uuid4()}.pdf")

    def enable_readium(self):
        """Switch LCP on without `Entry.post_save`, which would encrypt the entry's files by itself."""
        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, "readium_enabled": True})


class LedgerEventTests(Fixtures):
    def test_full_lifecycle_from_events(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-received", task_id, now, name="apps.core.tasks.store_table_of_contents"))
        ledger.apply_event(event("task-started", task_id, now + 1, hostname="catalog@dev.pod"))
        ledger.apply_event(event("task-succeeded", task_id, now + 5, result="{'pages': 3}", runtime=4.2))
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.status, Status.SUCCEEDED)
        self.assertEqual(job.result, {"pages": 3})
        self.assertEqual(job.runtime, 4.2)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.worker, "catalog@dev.pod")
        self.assertEqual(job.kind, "core.table_of_contents")

    def test_failure_keeps_exception_and_traceback(self):
        task_id = uuid.uuid4()
        ledger.apply_event(event("task-received", task_id, name="apps.tasks.tasks.backup"))
        ledger.apply_event(event("task-failed", task_id, exception="OSError('disk full')", traceback="Traceback…"))
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.status, Status.FAILED)
        self.assertEqual(job.error, "OSError('disk full')")
        self.assertEqual(job.traceback, "Traceback…")
        self.assertTrue(JobEvent.objects.filter(job=job, type="task-failed").exists())

    def test_older_event_does_not_overwrite_newer_state(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-succeeded", task_id, now))
        ledger.apply_event(event("task-started", task_id, now - 10))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.SUCCEEDED)

    def test_revoked_is_final(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-revoked", task_id, now))
        ledger.apply_event(event("task-received", task_id, now + 5))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.REVOKED)

    def test_redelivery_reopens_a_finished_job_and_counts_attempts(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-started", task_id, now))
        ledger.apply_event(event("task-failed", task_id, now + 1, exception="WorkerLostError"))
        ledger.apply_event(event("task-started", task_id, now + 2))
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.status, Status.STARTED)
        self.assertEqual(job.attempts, 2)
        self.assertIsNone(job.error)

    def test_retry_and_revocation_statuses(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-started", task_id, now))
        ledger.apply_event(event("task-retried", task_id, now + 1, exception="Timeout"))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.RETRYING)
        ledger.apply_event(event("task-rejected", task_id, now + 2))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.REJECTED)

    def test_sent_after_received_does_not_go_back_to_queued(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-received", task_id, now))
        ledger.apply_event(event("task-sent", task_id, now + 1, queue="celery"))
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.status, Status.RECEIVED)
        self.assertEqual(job.queue, "celery")

    def test_child_without_parent_row_is_kept(self):
        child = uuid.uuid4()
        ledger.apply_event(event("task-received", child, parent_id=str(uuid.uuid4()), name="x.y"))
        self.assertTrue(JobProtocol.objects.filter(pk=child).exists())

    def test_child_inherits_the_parents_subject(self):
        acquisition = self.acquisition()
        parent = uuid.uuid4()
        ledger.apply_event(
            event(
                "task-received",
                parent,
                name="evilflowers_analyzer.extract",
                kwargs=repr({"acquisition_id": str(acquisition.pk)}),
            )
        )
        child = uuid.uuid4()
        ledger.apply_event(
            event("task-received", child, name="evilflowers_text_worker.paragraphs", parent_id=str(parent))
        )
        job = JobProtocol.objects.get(pk=child)
        self.assertEqual(job.subject_id, acquisition.pk)
        self.assertEqual(job.entry_id, self.entry.pk)
        self.assertIsNone(job.kind)

    def test_secrets_are_redacted(self):
        task_id = uuid.uuid4()
        ledger.apply_event(
            event(
                "task-received",
                task_id,
                name="apps.dataverse.tasks.resume_workflow",
                kwargs=repr({"dataverse_token": "abc", "invocation_id": "1"}),
            )
        )
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.job_kwargs["dataverse_token"], ledger.REDACTED)
        self.assertEqual(job.job_kwargs["invocation_id"], "1")

    def test_ignored_tasks_are_not_recorded(self):
        task_id = uuid.uuid4()
        with override_settings(EVILFLOWERS_JOBS_IGNORE_TASKS=frozenset({"noisy.task"})):
            ledger.apply_event(event("task-received", task_id, name="noisy.task"))
        self.assertFalse(JobProtocol.objects.filter(pk=task_id).exists())


class RecordPublishTests(Fixtures):
    def test_publish_records_exact_arguments_and_subject(self):
        task_id = str(uuid.uuid4())
        headers = {"id": task_id, "task": "apps.core.tasks.store_table_of_contents"}
        ledger.record_publish(headers, ([str(self.entry.pk)], {}, {}), routing_key="celery")
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.status, Status.QUEUED)
        self.assertTrue(job.args_exact)
        self.assertEqual(job.job_args, [str(self.entry.pk)])
        self.assertEqual(job.kind, "core.table_of_contents")
        self.assertEqual(job.subject_id, self.entry.pk)
        self.assertEqual(job.subject_type, ContentType.objects.get_for_model(Entry))
        self.assertEqual(job.catalog_id, self.catalog.pk)

    def test_publish_with_a_secret_is_not_resendable(self):
        task_id = str(uuid.uuid4())
        headers = {"id": task_id, "task": "apps.dataverse.tasks.resume_workflow"}
        ledger.record_publish(headers, ([], {"dataverse_token": "t"}, {}))
        job = JobProtocol.objects.get(pk=task_id)
        self.assertFalse(job.args_exact)
        self.assertEqual(job.job_kwargs["dataverse_token"], ledger.REDACTED)

    def test_publish_of_a_finished_stable_id_starts_it_over(self):
        task_id = str(uuid.uuid4())
        JobProtocol.objects.create(id=task_id, name="evilflowers_analyzer.extract", status=Status.SUCCEEDED)
        ledger.record_publish({"id": task_id, "task": "evilflowers_analyzer.extract"}, ([], {}, {}))
        self.assertEqual(JobProtocol.objects.get(pk=task_id).status, Status.QUEUED)

    def test_publish_never_raises(self):
        ledger.record_publish({"id": "not-a-uuid", "task": "x"}, None)


class JobServiceTests(Fixtures):
    def test_start_writes_the_row_before_sending_under_its_id(self):
        with patch(SEND) as send:
            job = JobService.start("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(job.status, Status.QUEUED)
        self.assertEqual(job.requested_by, self.admin)
        self.assertEqual(send.call_args.kwargs["task_id"], str(job.pk))
        self.assertEqual(send.call_args.args[0], "apps.tasks.tasks.prune_jobs")
        self.assertTrue(JobEvent.objects.filter(job=job, type="queued").exists())

    def test_non_admin_is_refused(self):
        with self.assertRaises(JobRefused) as refused:
            JobService.start("maintenance.prune_jobs", user=self.reader)
        self.assertEqual(refused.exception.status, 403)

    def test_second_active_job_is_refused_unless_forced(self):
        with patch(SEND):
            JobService.start("maintenance.prune_jobs", user=self.admin)
            with self.assertRaises(JobRefused) as refused:
                JobService.start("maintenance.prune_jobs", user=self.admin)
            self.assertEqual(refused.exception.status, 409)
            JobService.start("maintenance.prune_jobs", user=self.admin, force=True)

    def test_unknown_options_are_refused(self):
        with self.assertRaises(JobRefused) as refused:
            JobService.start("maintenance.prune_jobs", user=self.admin, options={"bogus": 1})
        self.assertEqual(refused.exception.status, 422)

    def test_dispatch_failure_marks_the_job_failed(self):
        with patch(SEND, side_effect=ConnectionError("broker down")):
            with self.assertRaises(JobRefused) as refused:
                JobService.start("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(refused.exception.status, 502)
        job = JobProtocol.objects.get(kind="maintenance.prune_jobs")
        self.assertEqual(job.status, Status.FAILED)
        self.assertIn("broker down", job.error)

    def test_retry_of_a_kind_starts_a_new_job(self):
        with patch(SEND):
            job = JobService.start("maintenance.prune_jobs", user=self.admin)
            JobProtocol.objects.filter(pk=job.pk).update(status=Status.FAILED)
            job.refresh_from_db()
            retry = JobService.retry(job, user=self.admin)
        self.assertNotEqual(retry.pk, job.pk)
        self.assertEqual(retry.retry_of_id, job.pk)

    def test_retry_of_an_active_job_is_refused(self):
        with patch(SEND):
            job = JobService.start("maintenance.prune_jobs", user=self.admin)
        with self.assertRaises(JobRefused):
            JobService.retry(job, user=self.admin)

    def test_retry_resends_a_recorded_call_without_a_startable_kind(self):
        job = JobProtocol.objects.create(
            id=uuid.uuid4(),
            name="apps.notifications.tasks.send_notification",
            kind="notifications.send",
            status=Status.FAILED,
            job_args=["loan_ready", str(self.reader.pk), {}],
            args_exact=True,
        )
        with patch(SEND) as send:
            retry = JobService.retry(job, user=self.admin)
        self.assertEqual(send.call_args.args[0], "apps.notifications.tasks.send_notification")
        self.assertEqual(send.call_args.kwargs["args"], job.job_args)
        self.assertEqual(send.call_args.kwargs["task_id"], str(retry.pk))

    def test_retry_refuses_redacted_calls(self):
        job = JobProtocol.objects.create(
            id=uuid.uuid4(),
            name="apps.dataverse.tasks.resume_workflow",
            kind="dataverse.resume_workflow",
            status=Status.FAILED,
            args_exact=False,
        )
        with self.assertRaises(JobRefused):
            JobService.retry(job, user=self.admin)

    def test_revoke_of_a_queued_job_is_immediate(self):
        with patch(SEND):
            job = JobService.start("maintenance.prune_jobs", user=self.admin)
        with patch(REVOKE) as revoke:
            job = JobService.revoke(job, user=self.admin)
        revoke.assert_called_once_with(str(job.pk), terminate=False)
        self.assertEqual(job.status, Status.REVOKED)
        self.assertIsNotNone(job.revoke_requested_at)

    def test_running_job_needs_terminate_and_a_safe_kind(self):
        with patch(SEND):
            job = JobService.start("maintenance.prune_jobs", user=self.admin)
        JobProtocol.objects.filter(pk=job.pk).update(status=Status.STARTED)
        job.refresh_from_db()
        with self.assertRaises(JobRefused):
            JobService.revoke(job, user=self.admin)
        with self.assertRaises(JobRefused) as refused:
            JobService.revoke(job, user=self.admin, terminate=True)
        self.assertEqual(refused.exception.status, 422)

    def test_summary_counts_per_kind_and_status(self):
        with patch(SEND):
            JobService.start("maintenance.prune_jobs", user=self.admin)
        rows = JobService.summary(JobProtocol.objects.all())
        self.assertEqual(rows, [{"kind": "maintenance.prune_jobs", "statuses": {"queued": 1}, "stale": 0}])


@patch(PREPARE_SOURCE, side_effect=lambda ec: ec.acquisition.content.name)
class EncryptKindTests(Fixtures):
    def test_start_encrypts_under_the_job_id(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        with patch(BROKER) as broker:
            job = JobService.start("readium.encrypt", acquisition.pk, user=self.admin)
        _event, payload = broker.return_value.execute.call_args.args
        self.assertEqual(payload["task_id"], str(job.pk))
        encrypted = EncryptedContent.objects.get(acquisition=acquisition)
        self.assertEqual(encrypted.task_id, str(job.pk))
        self.assertEqual(job.entry_id, self.entry.pk)

    def test_registered_content_needs_the_reencrypt_action(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        with patch(BROKER):
            job = JobService.start("readium.encrypt", acquisition.pk, user=self.admin)
        EncryptedContent.objects.filter(acquisition=acquisition).update(
            status=EncryptedContent.EncryptionStatus.REGISTERED, encrypted_at=timezone.now()
        )
        JobProtocol.objects.filter(pk=job.pk).update(status=Status.SUCCEEDED)
        job.refresh_from_db()
        with self.assertRaises(JobRefused) as refused:
            JobService.retry(job, user=self.admin)
        self.assertEqual(refused.exception.status, 422)

        dry = JobService.run_action(job, "reencrypt", user=self.admin, options={"dry_run": True})
        self.assertTrue(dry["dry_run"])
        with patch(BROKER):
            result = JobService.run_action(job, "reencrypt", user=self.admin)
        new = JobProtocol.objects.get(pk=result["job_id"])
        self.assertEqual(new.retry_of_id, job.pk)
        self.assertEqual(EncryptedContent.objects.get(acquisition=acquisition).task_id, str(new.pk))

    def test_entry_trigger_does_not_send_a_healthy_pending_job_twice(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        headers = self.client_headers(self.admin)
        with patch(BROKER):
            first = self.trigger(headers)
            second = self.trigger(headers)
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(JobProtocol.objects.filter(kind="readium.encrypt", subject_id=acquisition.pk).count(), 1)

        # Stale: re-sent under the same content id.
        JobProtocol.objects.filter(kind="readium.encrypt").update(created_at=timezone.now() - timedelta(days=1))
        with patch(BROKER):
            third = self.trigger(headers)
        self.assertEqual(third.status_code, 202, third.content)

    def trigger(self, headers):
        return self.client.post(
            f"/readium/v1/entries/{self.entry.pk}/encryption",
            data="{}",
            content_type="application/json",
            headers=headers,
        )

    def client_headers(self, user):
        from apps.core.auth import JWTFactory
        from apps.core.models import ApiKey

        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def test_publish_of_an_encryption_task_is_attributed(self, _prepare):
        acquisition = self.acquisition()
        content = EncryptedContent.objects.create(
            acquisition=acquisition, lcp_content_id=str(uuid.uuid4()), encrypted_path="x"
        )
        task_id = str(uuid.uuid4())
        ledger.record_publish(
            {"id": task_id, "task": "evilflowers_lcpencrypt_worker.lcpencrypt"},
            ([], {"contentid": content.lcp_content_id}, {}),
        )
        job = JobProtocol.objects.get(pk=task_id)
        self.assertEqual(job.kind, "readium.encrypt")
        self.assertEqual(job.subject_id, acquisition.pk)

    def test_encryption_started_by_the_entry_signal_is_in_the_ledger(self, _prepare):
        # The pre-IP-031 path (Entry.post_save → encrypt_acquisition → event broker) is not changed;
        # the publish hook records it, with its kind and subject.
        entry = Entry.objects.create(
            creator=self.admin, catalog=self.catalog, title="Auto", config={"readium_enabled": True}
        )
        with patch("kombu.messaging.Producer.publish"):
            acquisition = Acquisition.objects.create(
                entry=entry, mime=Acquisition.AcquisitionMIME.PDF, content=f"catalogs/catalog/{uuid.uuid4()}.pdf"
            )
        encrypted = EncryptedContent.objects.get(acquisition=acquisition)
        job = JobProtocol.objects.get(pk=encrypted.task_id)
        self.assertEqual(job.kind, "readium.encrypt")
        self.assertEqual(job.subject_id, acquisition.pk)
        self.assertEqual(job.status, Status.QUEUED)


@override_settings(EVILFLOWERS_EXTRACTION_ENABLED=True)
class ExtractionKindTests(Fixtures):
    def test_stable_task_id_reuses_the_row(self):
        from apps.extraction.services import task_id

        acquisition = self.acquisition()
        with patch("apps.extraction.services.queue", return_value=object()):
            first = JobService.start("extraction.run", acquisition.pk, user=self.admin)
            self.assertEqual(str(first.pk), task_id(acquisition.pk, 2))
            JobProtocol.objects.filter(pk=first.pk).update(status=Status.FAILED, error="boom")
            first.refresh_from_db()
            again = JobService.retry(first, user=self.admin)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.status, Status.QUEUED)
        self.assertIsNone(again.error)
        self.assertIsNone(again.retry_of_id)

        # Workers remember revoked ids; a re-run under the same id would be discarded too.
        with self.assertRaises(JobRefused) as refused:
            JobService.revoke(again, user=self.admin)
        self.assertEqual(refused.exception.status, 422)


class HousekeepingTests(Fixtures):
    def test_sweeper_marks_silent_started_jobs_lost(self):
        old = timezone.now() - timedelta(days=1)
        job = JobProtocol.objects.create(
            name="apps.tasks.tasks.backup", status=Status.STARTED, last_event_at=old, started_at=old
        )
        fresh = JobProtocol.objects.create(
            name="apps.tasks.tasks.backup", status=Status.STARTED, last_event_at=timezone.now()
        )
        self.assertEqual(sweep_lost_jobs(), 1)
        job.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(job.status, Status.LOST)
        self.assertEqual(fresh.status, Status.STARTED)

    def test_queued_without_any_event_becomes_lost_after_the_queued_limit(self):
        old = timezone.now() - timedelta(days=3)
        missed = JobProtocol.objects.create(name="apps.tasks.tasks.backup", status=Status.QUEUED)
        waiting = JobProtocol.objects.create(name="apps.tasks.tasks.backup", status=Status.QUEUED)
        JobProtocol.objects.filter(pk=missed.pk).update(created_at=old)
        self.assertEqual(sweep_lost_jobs(), 1)
        missed.refresh_from_db()
        waiting.refresh_from_db()
        self.assertEqual(missed.status, Status.LOST)
        self.assertEqual(waiting.status, Status.QUEUED)

    def test_beat_runs_keep_no_event_history_unless_they_fail(self):
        task_id = uuid.uuid4()
        now = time.time()
        ledger.apply_event(event("task-received", task_id, now, name="apps.readium.tasks.expire_lapsed_licenses"))
        ledger.apply_event(event("task-succeeded", task_id, now + 1))
        self.assertFalse(JobEvent.objects.filter(job_id=task_id).exists())

    def test_service_tasks_get_the_longer_limit(self):
        three_hours = timezone.now() - timedelta(hours=3)
        job = JobProtocol.objects.create(
            name="evilflowers_equation.equations", status=Status.STARTED, last_event_at=three_hours
        )
        sweep_lost_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, Status.STARTED)

    def test_prune_keeps_the_latest_job_per_subject(self):
        acquisition = self.acquisition()
        content_type = ContentType.objects.get_for_model(Acquisition)
        old = timezone.now() - timedelta(days=400)
        older = JobProtocol.objects.create(
            name="t", kind="files.ocr", subject_type=content_type, subject_id=acquisition.pk, status=Status.SUCCEEDED
        )
        latest = JobProtocol.objects.create(
            name="t", kind="files.ocr", subject_type=content_type, subject_id=acquisition.pk, status=Status.SUCCEEDED
        )
        noise = JobProtocol.objects.create(name="beat", status=Status.SUCCEEDED)
        JobProtocol.objects.filter(pk__in=[older.pk, noise.pk]).update(created_at=old)
        JobProtocol.objects.filter(pk=latest.pk).update(created_at=old + timedelta(days=1))
        self.assertEqual(prune_jobs(), 2)
        self.assertTrue(JobProtocol.objects.filter(pk=latest.pk).exists())


class RegistryTests(TestCase):
    def test_every_catalog_task_has_a_kind(self):
        from evil_flowers_catalog.celery import app

        own = {name for name in app.tasks if name.startswith("apps.")}
        missing = sorted(name for name in own if registry.for_task(name) is None)
        self.assertEqual(missing, [])

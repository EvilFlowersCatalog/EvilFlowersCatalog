"""IP-031 rev. 2: the job facade (declare, reconcile, facts, observe, report) and the run ledger."""

import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, Language, User
from apps.readium.models import EncryptedContent, License
from apps.tasks.application.facade import facade
from apps.tasks.application.registry import registry
from apps.tasks.contract import DispatchError
from apps.tasks.domain.values import DesiredState, JobState, RunStatus
from apps.tasks.infrastructure import ledger
from apps.tasks.infrastructure.maintenance import prune_runs, sweep_lost_runs
from apps.tasks.models import Job, JobEvent, JobRun

BROKER = "apps.readium.services.content_encryption_service.get_event_broker"
PREPARE_SOURCE = "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source"


class FakeDispatcher:
    def __init__(self, fail=None):
        self.sent, self.revoked, self.fail = [], [], fail

    def send(self, task, *, task_id, args=None, kwargs=None, queue=None):
        if self.fail:
            raise self.fail
        self.sent.append({"task": task, "task_id": task_id, "args": args, "kwargs": kwargs, "queue": queue})

    def revoke(self, task_id, *, terminate=False):
        self.revoked.append((task_id, terminate))


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

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        patcher = patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def acquisition(self, mime=Acquisition.AcquisitionMIME.PDF):
        return Acquisition.objects.create(entry=self.entry, mime=mime, content=f"catalogs/catalog/{uuid.uuid4()}.pdf")

    def enable_readium(self):
        """LCP on without `Entry.post_save` (which would encrypt the entry's files by itself)."""
        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, "readium_enabled": True})


class DeclareTests(Fixtures):
    def test_apply_writes_the_job_and_dispatches_one_run_under_its_id(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(
            (job.state, job.generation, job.observed_generation, job.attempts), (JobState.QUEUED, 1, 1, 1)
        )
        (sent,) = self.dispatcher.sent
        self.assertEqual(sent["task"], "apps.tasks.tasks.prune_jobs")
        self.assertEqual(sent["task_id"], str(job.current_run_id))
        run = JobRun.objects.get(pk=job.current_run_id)
        self.assertEqual((run.job_id, run.status), (job.pk, RunStatus.QUEUED))
        self.assertTrue(JobEvent.objects.filter(job_ref=job, type="job:applied").exists())

    def test_apply_is_idempotent_while_the_job_is_on_its_way(self):
        first = facade.apply("maintenance.prune_jobs", user=self.admin)
        second = facade.apply("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.generation, 1)
        self.assertEqual(len(self.dispatcher.sent), 1)

    def test_apply_on_a_settled_job_is_the_retry(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        Job.objects.filter(pk=job.pk).update(state=JobState.FAILED)
        again = facade.apply("maintenance.prune_jobs", user=self.admin)
        self.assertEqual((again.generation, again.state), (2, JobState.QUEUED))
        self.assertEqual(len(self.dispatcher.sent), 2)

    def test_stale_job_gets_a_new_run(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        Job.objects.filter(pk=job.pk).update(last_transition_at=timezone.now() - timedelta(days=1))
        facade.apply("maintenance.prune_jobs", user=self.admin)
        self.assertEqual(len(self.dispatcher.sent), 2)

    def test_non_admin_unknown_kind_run_only_kind_and_bad_spec(self):
        cases = [
            (dict(kind="maintenance.prune_jobs", user=self.reader), 403),
            (dict(kind="nope", user=self.admin), 404),
            (dict(kind="notifications.send", user=self.admin), 409),
            (dict(kind="maintenance.prune_jobs", user=self.admin, spec={"bogus": 1}), 422),
        ]
        for arguments, status in cases:
            kind = arguments.pop("kind")
            with self.subTest(kind=kind), self.assertRaises(DispatchError) as refused:
                facade.apply(kind, **arguments)
            self.assertEqual(refused.exception.status, status)

    def test_permanent_refusal_is_422_without_a_job(self):
        acquisition = self.acquisition(mime=Acquisition.AcquisitionMIME.EPUB)
        with self.assertRaises(DispatchError) as refused, override_settings(EVILFLOWERS_OCR_ENABLED=True):
            facade.apply("files.ocr", acquisition.pk, user=self.admin)
        self.assertEqual(refused.exception.status, 422)
        self.assertFalse(Job.objects.filter(kind="files.ocr").exists())

    def test_transient_refusal_blocks_and_the_reconciler_proceeds_later(self):
        acquisition = self.acquisition()
        job = facade.apply("files.ocr", acquisition.pk, user=self.admin)  # OCR switched off: blocked
        self.assertEqual(job.state, JobState.BLOCKED)
        self.assertIn("EVILFLOWERS_OCR_ENABLED", job.reason)
        self.assertEqual(self.dispatcher.sent, [])
        with override_settings(EVILFLOWERS_OCR_ENABLED=True):
            self.assertEqual(facade.reconcile_due(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual(self.dispatcher.sent[0]["task"], "evilflowers_ocr_worker.ocr")

    def test_dry_run_stores_nothing(self):
        acquisition = self.acquisition()
        plan = facade.apply("files.ocr", acquisition.pk, user=self.admin, dry_run=True)
        self.assertEqual(plan.outcome, "would-block")
        self.assertFalse(Job.objects.exists())

    def test_dispatch_failure_fails_the_job(self):
        self.dispatcher.fail = ConnectionError("broker down")
        with self.assertLogs("apps.tasks.audit", "ERROR"):
            job = facade.apply("maintenance.prune_jobs", user=self.admin)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("broker down", job.reason)


class CancelTests(Fixtures):
    def test_cancel_a_queued_job_revokes_its_run(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        job = facade.patch(job.pk, desired=DesiredState.CANCELLED, user=self.admin)
        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertEqual(self.dispatcher.revoked, [(str(job.current_run_id), False)])
        self.assertEqual(JobRun.objects.get(pk=job.current_run_id).status, RunStatus.REVOKED)

    def test_cancel_a_running_job_waits_for_its_run(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        run = job.current_run
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-started", run.pk))
        job = facade.patch(job.pk, desired=DesiredState.CANCELLED, user=self.admin)
        self.assertEqual(job.state, JobState.CANCELLING)
        self.assertEqual(self.dispatcher.revoked, [])  # maintenance runs are not terminate_safe
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-succeeded", run.pk, time.time() + 1))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.CANCELLED)

    def test_cancelled_job_resumes_with_a_new_generation(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        facade.patch(job.pk, desired=DesiredState.CANCELLED, user=self.admin)
        job = facade.patch(job.pk, desired=DesiredState.COMPLETED, user=self.admin)
        self.assertEqual((job.state, job.generation), (JobState.QUEUED, 2))


class RunFactsTests(Fixtures):
    def test_events_move_the_run_and_its_job(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        run_id, now = job.current_run_id, time.time()
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-started", run_id, now, hostname="catalog@dev"))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.RUNNING)
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-succeeded", run_id, now + 1, result="{'deleted': 3}", runtime=0.5))
        job.refresh_from_db()
        run = JobRun.objects.get(pk=run_id)
        self.assertEqual(
            (job.state, run.status, run.result), (JobState.SUCCEEDED, RunStatus.SUCCEEDED, {"deleted": 3})
        )

    def test_failure_keeps_the_error_and_settles_the_job(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(
                event("task-failed", job.current_run_id, exception="OSError('disk full')", traceback="Tb")
            )
        job.refresh_from_db()
        self.assertEqual((job.state, job.reason), (JobState.FAILED, "OSError('disk full')"))
        self.assertEqual(JobRun.objects.get(pk=job.current_run_id).traceback, "Tb")

    def test_run_sent_outside_the_facade_gets_its_kinds_job_without_intent(self):
        task_id = str(uuid.uuid4())
        with self.captureOnCommitCallbacks(execute=True):
            ledger.record_publish(
                {"id": task_id, "task": "apps.core.tasks.store_table_of_contents"}, ([str(self.entry.pk)], {}, {})
            )
        run = JobRun.objects.get(pk=task_id)
        job = Job.objects.get(kind="core.table_of_contents", subject_id=self.entry.pk)
        self.assertEqual((run.job_id, run.args_exact, run.job_args), (job.pk, True, [str(self.entry.pk)]))
        self.assertEqual((job.generation, job.observed_generation), (0, 0))  # no intent: never dispatches
        self.assertEqual(job.subject_type, ContentType.objects.get_for_model(Entry))
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-succeeded", task_id))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.SUCCEEDED)

    def test_beat_runs_attach_to_one_job_per_kind(self):
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                ledger.record_publish(
                    {"id": str(uuid.uuid4()), "task": "apps.readium.tasks.expire_lapsed_licenses"}, ([], {}, {})
                )
        self.assertEqual(Job.objects.filter(kind="readium.expire_lapsed_licenses").count(), 1)
        self.assertEqual(JobRun.objects.filter(kind="readium.expire_lapsed_licenses").count(), 2)

    def test_run_only_kinds_record_runs_without_jobs(self):
        task_id = str(uuid.uuid4())
        with self.captureOnCommitCallbacks(execute=True):
            ledger.record_publish(
                {"id": task_id, "task": "apps.notifications.tasks.send_notification"},
                (["loan_ready", str(self.reader.pk), {}], {}, {}),
            )
        run = JobRun.objects.get(pk=task_id)
        self.assertIsNone(run.job_id)
        self.assertEqual(run.subject_id, self.reader.pk)

    def test_follow_up_tasks_belong_to_their_parents_job_but_do_not_move_it(self):
        job = facade.apply("maintenance.prune_jobs", user=self.admin)
        child = uuid.uuid4()
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-failed", child, name="x.follow_up", parent_id=str(job.current_run_id)))
        self.assertEqual(JobRun.objects.get(pk=child).job_id, job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.QUEUED)

    def test_secrets_are_redacted_and_never_resendable(self):
        task_id = str(uuid.uuid4())
        ledger.record_publish(
            {"id": task_id, "task": "apps.dataverse.tasks.resume_workflow"},
            ([], {"dataverse_token": "t", "invocation_id": "1"}, {}),
        )
        run = JobRun.objects.get(pk=task_id)
        self.assertEqual(run.job_kwargs["dataverse_token"], ledger.REDACTED)
        self.assertFalse(run.args_exact)

    def test_precedence_older_events_are_history(self):
        task_id, now = uuid.uuid4(), time.time()
        ledger.apply_event(event("task-succeeded", task_id, now, name="x.y"))
        ledger.apply_event(event("task-started", task_id, now - 10))
        self.assertEqual(JobRun.objects.get(pk=task_id).status, RunStatus.SUCCEEDED)


@patch(PREPARE_SOURCE, side_effect=lambda ec: ec.acquisition.content.name)
class EncryptObservationTests(Fixtures):
    def test_apply_encrypts_under_the_run_id(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        with patch(BROKER) as broker:
            job = facade.apply("readium.encrypt", acquisition.pk, user=self.admin)
        _event, payload = broker.return_value.execute.call_args.args
        self.assertEqual(payload["task_id"], str(job.current_run_id))
        self.assertEqual(EncryptedContent.objects.get(acquisition=acquisition).task_id, str(job.current_run_id))

    def test_lcp_off_blocks_but_a_pending_package_is_still_resent(self, _prepare):
        acquisition = self.acquisition()
        job = facade.apply("readium.encrypt", acquisition.pk, user=self.admin)
        self.assertEqual(job.state, JobState.BLOCKED)
        EncryptedContent.objects.create(acquisition=acquisition, lcp_content_id=str(uuid.uuid4()), encrypted_path="x")
        with patch(BROKER):
            job = facade.reconcile(job.pk)
        self.assertEqual(job.state, JobState.QUEUED)

    def test_reencrypt_waits_for_active_loans(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        content = EncryptedContent.objects.create(
            acquisition=acquisition,
            lcp_content_id=str(uuid.uuid4()),
            encrypted_path="x",
            status=EncryptedContent.EncryptionStatus.REGISTERED,
            encrypted_at=timezone.now(),
        )
        now = timezone.now()
        License.objects.create(
            entry=self.entry,
            user=self.reader,
            encrypted_content=content,
            state=License.LicenseState.ACTIVE,
            starts_at=now,
            expires_at=now + timedelta(days=1),
        )
        with self.assertRaises(DispatchError):  # registered without reencrypt: refused, no job
            facade.apply("readium.encrypt", acquisition.pk, user=self.admin)
        job = facade.apply("readium.encrypt", acquisition.pk, spec={"reencrypt": True}, user=self.admin)
        self.assertEqual(job.state, JobState.BLOCKED)
        self.assertIn("1 active loan", job.reason)
        with patch(BROKER):
            forced = facade.apply(
                "readium.encrypt", acquisition.pk, spec={"reencrypt": True, "force": True}, user=self.admin
            )
        self.assertEqual(forced.state, JobState.QUEUED)

    def test_domain_state_is_observed_and_discovered(self, _prepare):
        acquisition = self.acquisition()
        content = EncryptedContent.objects.create(
            acquisition=acquisition,
            lcp_content_id=str(uuid.uuid4()),
            encrypted_path="x",
            status=EncryptedContent.EncryptionStatus.FAILED,
            error_message="lcpencrypt exited 1",
        )
        self.assertEqual(facade.discover("readium.encrypt"), 1)
        job = Job.objects.get(kind="readium.encrypt", subject_id=acquisition.pk)
        self.assertEqual((job.state, job.reason, job.generation), (JobState.FAILED, "lcpencrypt exited 1", 0))
        self.assertEqual(job.detail["encryption"]["content_id"], content.lcp_content_id)
        # A webhook registering the package is observed through post_save.
        with self.captureOnCommitCallbacks(execute=True):
            content.status = EncryptedContent.EncryptionStatus.REGISTERED
            content.save()
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.SUCCEEDED)
        self.assertEqual(facade.reconcile_due(), 0)  # observed jobs carry no intent

    def test_succeeded_run_with_a_pending_package_is_not_a_succeeded_job(self, _prepare):
        acquisition = self.acquisition()
        self.enable_readium()
        with patch(BROKER):
            job = facade.apply("readium.encrypt", acquisition.pk, user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event("task-succeeded", job.current_run_id))
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.QUEUED)  # EncryptedContent still pending: the domain wins


@override_settings(EVILFLOWERS_EXTRACTION_ENABLED=True)
class StableTaskIdTests(Fixtures):
    def test_every_run_reuses_the_run_row(self):
        from apps.extraction.services import task_id

        acquisition = self.acquisition()
        with patch("apps.extraction.services.queue", return_value=object()):
            job = facade.apply("extraction.run", acquisition.pk, user=self.admin)
            self.assertEqual(str(job.current_run_id), task_id(acquisition.pk, 2))
            self.assertEqual(str(job.current_run_id), str(job.current_run.pk))
            with self.captureOnCommitCallbacks(execute=True):
                ledger.apply_event(event("task-failed", job.current_run_id, exception="boom"))
            again = facade.apply("extraction.run", acquisition.pk, user=self.admin)
        self.assertEqual(str(again.current_run_id), str(job.current_run_id))
        self.assertEqual((again.generation, again.state), (2, JobState.QUEUED))
        self.assertEqual(JobRun.objects.get(pk=job.current_run_id).status, RunStatus.QUEUED)

    def test_cancelling_never_revokes_a_stable_id(self):
        acquisition = self.acquisition()
        with patch("apps.extraction.services.queue", return_value=object()):
            job = facade.apply("extraction.run", acquisition.pk, user=self.admin)
        facade.patch(job.pk, desired=DesiredState.CANCELLED, user=self.admin)
        self.assertEqual(self.dispatcher.revoked, [])


class HousekeepingTests(Fixtures):
    def test_sweeper_loses_silent_runs_and_fails_their_job(self):
        job = facade.apply("maintenance.backup", user=self.admin)
        old = timezone.now() - timedelta(days=1)
        JobRun.objects.filter(pk=job.current_run_id).update(status=RunStatus.STARTED, last_event_at=old)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(sweep_lost_runs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.FAILED)

    def test_prune_keeps_every_jobs_current_run(self):
        job = facade.apply("maintenance.backup", user=self.admin)
        old_run = JobRun.objects.create(name="apps.tasks.tasks.backup", job=job, status=RunStatus.SUCCEEDED)
        noise = JobRun.objects.create(name="beat", status=RunStatus.SUCCEEDED)
        JobRun.objects.filter(pk__in=[old_run.pk, noise.pk, job.current_run_id]).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        JobRun.objects.filter(pk=job.current_run_id).update(status=RunStatus.SUCCEEDED)
        self.assertEqual(prune_runs(), 2)
        self.assertTrue(JobRun.objects.filter(pk=job.current_run_id).exists())


class RegistryTests(TestCase):
    def test_every_catalog_task_has_a_definition(self):
        from evil_flowers_catalog.celery import app

        own = {name for name in app.tasks if name.startswith("apps.")} - ledger.ALWAYS_IGNORED
        self.assertEqual(sorted(name for name in own if registry.for_task(name) is None), [])

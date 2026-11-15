"""#146/#169/#156: OCR takes its outcome from a file in shared storage plus the run's Celery event.

The worker writes `<entry>/ocr/<run>.pdf`, then `<entry>/ocr/<run>.json` carrying the run's capability token.
A terminal event (or the reconcile tick, when the event was lost) makes the catalog apply the file once.
"""

import json
import time
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings

from apps.core.models import Acquisition, Entry
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.files.jobs import queue_ocr
from apps.files.storage import get_storage
from apps.tasks.application.facade import facade
from apps.tasks.capability_scopes import JOB_RUN_OUTCOME, SYSTEM_SUBJECT
from apps.tasks.domain.values import JobState, RunStatus
from apps.tasks.infrastructure import ledger
from apps.tasks.models import Job, JobRun
from apps.tasks.tests.test_jobs import event
from apps.tasks.tests.test_jobs_api import ApiFixtures


@override_settings(EVILFLOWERS_OCR_ENABLED=True)
class OcrOutcomeTests(ApiFixtures):
    def apply(self, acquisition, spec=None):
        job = facade.apply("files.ocr", acquisition.pk, spec=spec, user=self.admin)
        return job, self.dispatcher.sent[-1]["kwargs"]

    def write(self, kwargs, *, save_pdf=True, **fields):
        storage = get_storage()
        if save_pdf:
            storage.save(kwargs["destination"], ContentFile(b"%PDF-1.4 ocr"))
        body = {
            "run_id": kwargs["run_id"],
            "token": kwargs["token"],
            "status": "done",
            "output": kwargs["destination"],
            "pages": 3,
            "pages_ocred": 2,
            "warnings": [],
            "engine": "tesseract",
            "engine_version": "5.5.0",
            **fields,
        }
        storage.save(kwargs["outcome"], ContentFile(json.dumps(body).encode()))

    def finish(self, run_id, kind="task-succeeded", **fields):
        with self.captureOnCommitCallbacks(execute=True):
            ledger.apply_event(event(kind, run_id, time.time() + 1, name="evilflowers_ocr_worker.ocr", **fields))
        return JobRun.objects.get(pk=run_id)

    def exists(self, path):
        return get_storage().exists(path)

    # -- dispatch -------------------------------------------------------------------------------------

    def test_dispatch_contract(self):
        acquisition = self.acquisition()
        job, kwargs = self.apply(acquisition)
        run_id = str(job.current_run_id)
        base = acquisition.upload_base_path()
        self.assertEqual(kwargs["destination"], f"{base}/ocr/{run_id}.pdf")
        self.assertEqual(kwargs["outcome"], f"{base}/ocr/{run_id}.json")
        self.assertEqual((kwargs["language"], kwargs["job_id"], kwargs["run_id"]), ("slk", str(job.pk), run_id))
        self.assertNotIn("report_url", kwargs)
        self.assertIsNone(kwargs["engine"])
        grant = CapabilityTokenService.peek(kwargs["token"], JOB_RUN_OUTCOME)
        self.assertEqual(
            (grant["resource_id"], grant["kind"], grant["sub"]), (run_id, "files.ocr", str(self.admin.pk))
        )
        self.assertTrue(grant["single_use"])

    def configure(self, **config):
        # After `self.acquisition()`: saving an upload saves the (cached) entry again.
        Entry.objects.filter(pk=self.entry.pk).update(config={**self.entry.config, **config})

    def test_system_declared_jobs_mint_for_the_system(self):
        acquisition = self.acquisition()
        self.configure(evilflowers_ocr_enabled=True)
        with self.captureOnCommitCallbacks(execute=True):
            queue_ocr(Acquisition.objects.get(pk=acquisition.pk))
        job = Job.objects.get(kind="files.ocr")
        self.assertIsNone(job.requested_by)
        self.assertEqual(job.state, JobState.QUEUED)
        grant = CapabilityTokenService.peek(self.dispatcher.sent[-1]["kwargs"]["token"], JOB_RUN_OUTCOME)
        self.assertEqual(grant["sub"], SYSTEM_SUBJECT)

    def test_upload_declares_ocr_only_when_the_entry_asks(self):
        with self.captureOnCommitCallbacks(execute=True):
            queue_ocr(self.acquisition())
        self.assertFalse(Job.objects.filter(kind="files.ocr").exists())

    def test_entry_config_form_takes_the_engine_and_leaves_it_alone_when_omitted(self):
        from apps.api.forms.entries import EntryConfigForm

        stored = EntryConfigForm({"evilflowers_ocr_engine": "rapidocr"})
        self.assertTrue(stored.is_valid())
        self.assertEqual(stored.cleaned_data, {"evilflowers_ocr_engine": "rapidocr"})
        partial = EntryConfigForm({"readium_amount": 2})
        self.assertTrue(partial.is_valid())
        self.assertNotIn("evilflowers_ocr_engine", partial.cleaned_data)  # the PUT merge keeps the stored one
        self.assertFalse(EntryConfigForm({"evilflowers_ocr_engine": "abbyy"}).is_valid())

    def test_an_outcome_for_a_deleted_acquisition_is_discarded(self):
        acquisition = self.acquisition()
        job, kwargs = self.apply(acquisition)
        # As the real dispatcher does: the publish hook stores the kwargs, so the paths survive the subject.
        ledger.record_publish({"id": kwargs["run_id"], "task": "evilflowers_ocr_worker.ocr"}, [[], kwargs, {}])
        self.write(kwargs)
        Acquisition.objects.filter(pk=acquisition.pk).delete()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(facade.apply_outcome(kwargs["run_id"]), None)  # still running: nothing yet
            JobRun.objects.filter(pk=kwargs["run_id"]).update(status=RunStatus.SUCCEEDED)
            self.assertEqual(facade.apply_outcome(kwargs["run_id"]), "discarded")
        self.assertFalse(self.exists(kwargs["outcome"]))

    def test_engine_from_spec_then_entry_config(self):
        first, second = self.acquisition(), self.acquisition()
        self.configure(evilflowers_ocr_engine="tesseract")
        _, kwargs = self.apply(Acquisition.objects.get(pk=first.pk), spec={"engine": "rapidocr"})
        self.assertEqual(kwargs["engine"], "rapidocr")  # the spec wins
        _, kwargs = self.apply(Acquisition.objects.get(pk=second.pk))
        self.assertEqual(kwargs["engine"], "tesseract")  # else the entry's
        with self.assertRaisesMessage(Exception, "engine"):
            facade.apply("files.ocr", first.pk, spec={"engine": "abbyy"}, user=self.admin)

    def test_the_ledger_never_stores_the_token(self):
        acquisition = self.acquisition()
        job, kwargs = self.apply(acquisition)
        ledger.record_publish({"id": kwargs["run_id"], "task": "evilflowers_ocr_worker.ocr"}, [[], kwargs, {}])
        stored = JobRun.objects.get(pk=kwargs["run_id"]).job_kwargs
        self.assertEqual(stored["token"], ledger.REDACTED)
        self.assertEqual(stored["outcome"], kwargs["outcome"])

    # -- apply ----------------------------------------------------------------------------------------

    def test_succeeded_event_applies_once_and_declares_follow_ups(self):
        created = self.acquisition()
        self.configure(evilflowers_ocr_rewrite=True, readium_enabled=True)
        acquisition = Acquisition.objects.get(pk=created.pk)
        original = acquisition.content.name
        job, kwargs = self.apply(acquisition)
        self.write(kwargs)
        with (
            patch("apps.readium.services.content_encryption_service.get_event_broker"),
            patch(
                "apps.readium.services.content_encryption_service.ContentEncryptionService._prepare_source",
                side_effect=lambda ec: ec.acquisition.content.name,
            ),
        ):
            run = self.finish(job.current_run_id)
        self.assertIsNotNone(run.reported_at)
        self.assertEqual((run.report["applied"], run.report["original"]), ("replaced", original))
        self.assertEqual((run.report["engine"], run.report["engine_version"]), ("tesseract", "5.5.0"))
        acquisition.refresh_from_db()
        self.assertEqual((acquisition.content.name, acquisition.checksum_cached), (kwargs["destination"], None))
        self.assertEqual(Job.objects.get(kind="readium.encrypt", subject_id=acquisition.pk).cause_id, job.pk)
        self.assertEqual(Job.objects.get(pk=job.pk).state, JobState.SUCCEEDED)
        self.assertTrue(self.exists(kwargs["destination"]))  # now the acquisition's file
        self.assertFalse(self.exists(kwargs["outcome"]))
        self.assertIsNone(CapabilityTokenService.peek(kwargs["token"], JOB_RUN_OUTCOME))  # consumed

        # A second event or a tick changes nothing.
        reported_at = run.reported_at
        self.write(kwargs, save_pdf=False)
        self.assertIsNone(facade.apply_outcome(run.pk))
        self.assertEqual(facade.apply_pending_outcomes(), 0)
        self.assertEqual(JobRun.objects.get(pk=run.pk).reported_at, reported_at)
        self.assertTrue(self.exists(kwargs["destination"]))

    def test_without_rewrite_the_output_becomes_a_derived_acquisition(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs)
        report = self.finish(job.current_run_id).report
        self.assertEqual(report["applied"], "derived")
        derived = Acquisition.objects.get(pk=report["derived_acquisition_id"])
        self.assertEqual(derived.content.name, kwargs["destination"])
        self.assertTrue(self.exists(kwargs["destination"]))

    def test_a_lost_event_is_applied_by_the_tick(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs)
        # The worker finished, but the listener missed the event: the sweeper calls the run lost.
        JobRun.objects.filter(pk=job.current_run_id).update(status=RunStatus.LOST)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(facade.apply_pending_outcomes(), 1)
        run = JobRun.objects.get(pk=job.current_run_id)
        self.assertEqual((run.status, run.report["applied"]), (RunStatus.SUCCEEDED, "derived"))
        self.assertEqual(Job.objects.get(pk=job.pk).state, JobState.SUCCEEDED)

    def test_no_file_yet_waits_for_the_tick(self):
        job, kwargs = self.apply(self.acquisition())
        run = self.finish(job.current_run_id)
        self.assertIsNone(run.reported_at)
        self.write(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(facade.apply_pending_outcomes(), 1)
        self.assertIsNotNone(JobRun.objects.get(pk=run.pk).reported_at)

    def assert_rejected(self, job, kwargs, reason):
        run = JobRun.objects.get(pk=job.current_run_id)
        self.assertEqual(run.report["status"], "outcome_rejected")
        self.assertIn(reason, run.report["reason"])
        self.assertEqual(run.status, RunStatus.FAILED)
        job = Job.objects.get(pk=job.pk)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn(reason, job.reason)
        self.assertFalse(self.exists(kwargs["destination"]))
        self.assertFalse(self.exists(kwargs["outcome"]))

    def test_another_runs_file_is_rejected(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs, run_id="00000000-0000-0000-0000-000000000000")
        self.finish(job.current_run_id)
        self.assert_rejected(job, kwargs, "another run")

    def test_an_unknown_or_expired_token_is_rejected(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs, token="forged")
        self.finish(job.current_run_id)
        self.assert_rejected(job, kwargs, "token")

    def test_a_token_minted_for_another_run_is_rejected(self):
        job, kwargs = self.apply(self.acquisition())
        other = CapabilityTokenService.mint(JOB_RUN_OUTCOME, {"sub": "system", "resource_id": "x"}, ttl=60)
        self.write(kwargs, token=other)
        self.finish(job.current_run_id)
        self.assert_rejected(job, kwargs, "another run")

    def test_an_output_other_than_the_destination_is_rejected(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs, output="catalogs/other/secret.pdf")
        self.finish(job.current_run_id)
        self.assert_rejected(job, kwargs, "output")

    def test_a_cancelled_run_is_discarded(self):
        job, kwargs = self.apply(self.acquisition())
        facade.patch(job.pk, desired="cancelled", user=self.admin)
        self.write(kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(facade.apply_pending_outcomes(), 1)
        run = JobRun.objects.get(pk=job.current_run_id)
        self.assertEqual(run.report["status"], "discarded")
        self.assertEqual(Job.objects.get(pk=job.pk).state, JobState.CANCELLED)
        self.assertFalse(self.exists(kwargs["destination"]))
        self.assertFalse(self.exists(kwargs["outcome"]))
        self.assertFalse(Acquisition.objects.filter(content=kwargs["destination"]).exists())

    def test_a_replaced_run_is_discarded(self):
        acquisition = self.acquisition()
        job, old = self.apply(acquisition)
        JobRun.objects.filter(pk=old["run_id"]).update(status=RunStatus.LOST)
        facade.run_changed(JobRun.objects.get(pk=old["run_id"]))
        job, new = self.apply(acquisition, spec={"redo": True})
        self.assertNotEqual(new["run_id"], old["run_id"])
        self.write(old)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(facade.apply_outcome(old["run_id"]), "discarded")
        self.assertFalse(self.exists(old["destination"]))
        self.assertEqual(Job.objects.get(pk=job.pk).current_run_id, job.current_run_id)
        self.assertNotEqual(Job.objects.get(pk=job.pk).state, JobState.FAILED)

    def test_a_failed_outcome_fails_the_job_with_its_error(self):
        job, kwargs = self.apply(self.acquisition())
        self.write(kwargs, save_pdf=False, status="failed", output=None, error="tesseract: language 'slk' missing")
        run = self.finish(job.current_run_id, kind="task-failed", exception="OcrFailed('see outcome')")
        self.assertEqual(run.report["status"], "failed")
        job = Job.objects.get(pk=job.pk)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.reason, "tesseract: language 'slk' missing")
        self.assertFalse(self.exists(kwargs["outcome"]))
        self.assertFalse(self.exists(kwargs["destination"]))

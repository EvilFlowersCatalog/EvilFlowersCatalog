"""#183: re-run single steps of a failed run without resetting the others."""

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.extraction import services
from apps.extraction.jobs import ExtractionRunSpec
from apps.extraction.models import ExtractionRun, Paragraph, Region
from apps.tasks.application.facade import facade
from apps.tasks.contract import DispatchError
from apps.tasks.domain.values import JobState
from apps.tasks.models import Job

BOX = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}
SIZES = [[595, 842]] * 16


def paragraph(seq: int, page: int = 1) -> dict:
    return {"page": page, "seq": seq, "bbox": BOX, "text": f"Paragraph number {seq} of the book.", "sentences": []}


def region(kind: str, page: int, seq: int = 0, image_path=None) -> dict:
    return {"kind": kind, "page": page, "seq": seq, "bbox": BOX, "image_path": image_path}


class FakeDispatcher:
    def __init__(self):
        self.sent = []

    def send(self, task, *, task_id, args=None, kwargs=None, queue=None):
        self.sent.append({"task": task, "task_id": task_id, "kwargs": kwargs, "queue": queue})

    def revoke(self, task_id, *, terminate=False):
        pass


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="admin", name="A", surname="D", auth_source=source, is_superuser=True)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.user.user_permissions.add(Permission.objects.get(codename="ingest_extraction"))
        catalog = Catalog.objects.create(creator=cls.user, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(creator=cls.user, catalog=catalog, title="Book")
        cls.acquisition = Acquisition.objects.create(
            entry=cls.entry, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/open/book.pdf"
        )

    def setUp(self):
        self.run = ExtractionRun.objects.create(
            acquisition=self.acquisition, pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        )

    def failed_run(self):
        """The dev pattern: figures and equations done, paragraphs and tables pending, failed by the stale sweep."""
        jobs = ["paragraphs", "figures:0001-0008", "figures:0009-0016", "tables", "equations"]
        attempt = services.start(self.run, page_count=16, page_sizes=SIZES, jobs=jobs, provenance={}).attempt
        services.add_regions(
            self.run,
            [region("figure", 2, image_path="crops/f2.webp"), region("figure", 12, image_path="crops/f12.webp")],
            attempt=attempt,
        )
        services.add_regions(self.run, [region("equation", 3)], attempt=attempt)
        services.add_regions(self.run, [region("table", 5, image_path="crops/t5.webp")], attempt=attempt)
        services.add_paragraphs(self.run, [paragraph(0)], [], attempt=attempt)  # a partial paragraphs pass
        for job in ("figures:0001-0008", "figures:0009-0016", "equations"):
            services.report_job(self.run, job, failed=False, error=None, provenance={"model": "m"}, attempt=attempt)
        # As the stale sweep does: on the run as the reports left it.
        self.run.refresh_from_db()
        services.fail(self.run, "no report for 24 h, pending: paragraphs, tables")
        self.run.refresh_from_db()
        return attempt

    def start_steps(self, steps, jobs):
        return services.start(
            self.run, page_count=16, page_sizes=SIZES, jobs=jobs, provenance={"analyzer": {"x": 1}}, steps=steps
        )


class StepStartTests(Base):
    def test_a_step_start_resets_only_the_named_steps(self):
        old = self.failed_run()
        with mock.patch.object(services, "delete_crops") as delete_crops:
            with self.captureOnCommitCallbacks(execute=True):
                run = self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables"])

        self.assertEqual(run.status, ExtractionRun.Status.RUNNING)
        self.assertEqual(run.attempt, old + 1)
        self.assertIsNone(run.error)
        self.assertEqual(
            run.jobs,
            {
                "figures:0001-0008": "done",
                "figures:0009-0016": "done",
                "equations": "done",
                "paragraphs": "pending",
                "tables": "pending",
            },
        )
        # The kept steps' rows stay, the named ones' are gone, their crops with them.
        self.assertEqual(sorted(run.regions.values_list("kind", flat=True)), ["equation", "figure", "figure"])
        self.assertFalse(Paragraph.objects.filter(run=run).exists())
        delete_crops.assert_called_once_with(["crops/t5.webp"])
        self.assertEqual(run.provenance["figures"], {"model": "m"})

    def test_failed_jobs_go_back_to_pending_under_the_names_declared_now(self):
        self.failed_run()
        ExtractionRun.objects.filter(pk=self.run.pk).update(
            jobs={"paragraphs": "done", "tables": "done", "equations": "done", "figures:0001-0008": "failed"}
        )
        self.run.refresh_from_db()
        # The analyzer batches differently now: both names replace the old one.
        run = self.start_steps(["figures"], ["figures:0001-0004", "figures:0005-0016"])
        self.assertEqual(
            run.jobs,
            {
                "paragraphs": "done",
                "tables": "done",
                "equations": "done",
                "figures:0001-0004": "pending",
                "figures:0005-0016": "pending",
            },
        )
        self.assertFalse(run.regions.filter(kind=Region.Kind.FIGURE).exists())
        self.assertTrue(run.regions.filter(kind=Region.Kind.TABLE).exists())

    def test_results_of_the_old_attempt_are_refused(self):
        old = self.failed_run()
        run = self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables"])
        # A stale task of a re-run step.
        with self.assertRaises(services.RunSuperseded):
            services.add_paragraphs(run, [paragraph(1)], [], attempt=old)
        with self.assertRaises(services.RunSuperseded):
            services.report_job(run, "tables", failed=False, error=None, provenance={}, attempt=old)
        # A duplicate report of a kept step.
        with self.assertRaises(services.RunSuperseded):
            services.report_job(run, "equations", failed=True, error="dup", provenance={}, attempt=old)
        run.refresh_from_db()
        self.assertEqual(run.jobs["tables"], "pending")
        self.assertEqual(run.jobs["equations"], "done")
        self.assertFalse(run.paragraphs.exists())

    def test_the_last_step_report_finishes_the_run_as_the_active_one(self):
        self.failed_run()
        run = self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables"])
        services.add_paragraphs(run, [paragraph(0), paragraph(1, page=2)], [], attempt=run.attempt)
        services.add_regions(run, [region("table", 5)], attempt=run.attempt)
        services.report_job(run, "tables", failed=False, error=None, provenance={}, attempt=run.attempt)
        with mock.patch("apps.extraction.tasks.queue_post_process") as post_process:
            with self.captureOnCommitCallbacks(execute=True):
                services.report_job(run, "paragraphs", failed=False, error=None, provenance={}, attempt=run.attempt)

        run.refresh_from_db()
        self.assertEqual(run.status, ExtractionRun.Status.DONE)
        self.assertTrue(run.is_active)
        self.assertIsNone(run.post_processed)
        post_process.assert_called_once_with(run.pk)
        # Locators are assigned to the paragraphs that came back.
        self.assertFalse(run.paragraphs.filter(locator_key__isnull=True).exists())
        self.assertEqual(run.regions.count(), 4)

    def test_another_unfinished_step_must_be_named(self):
        self.failed_run()
        with self.assertRaises(services.StepsRefused) as raised:
            self.start_steps(["paragraphs"], ["paragraphs"])
        self.assertIn("tables", str(raised.exception))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.FAILED)
        self.assertTrue(self.run.paragraphs.exists())

    def test_a_step_start_may_declare_only_the_named_steps(self):
        self.failed_run()
        with self.assertRaises(services.StepsRefused):
            self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables", "figures"])
        with self.assertRaises(services.StepsRefused):
            self.start_steps(["paragraphs", "tables"], ["paragraphs"])

    def test_a_full_start_of_a_failed_run_is_still_refused(self):
        """What an analyzer that ignores `steps` does: nothing is lost."""
        self.failed_run()
        with self.assertRaises(services.RunNotRunning):
            services.start(self.run, page_count=16, page_sizes=SIZES, jobs=["paragraphs"], provenance={})
        self.assertEqual(self.run.regions.count(), 4)

    def test_a_redelivered_step_pass_starts_the_steps_again(self):
        self.failed_run()
        first = self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables"]).attempt
        services.add_paragraphs(self.run, [paragraph(0)], [], attempt=first)
        services.report_job(self.run, "paragraphs", failed=False, error=None, provenance={}, attempt=first)
        self.run.refresh_from_db()

        second = self.start_steps(["paragraphs", "tables"], ["paragraphs", "tables"])
        self.assertEqual(second.attempt, first + 1)
        self.assertEqual(second.jobs["paragraphs"], "pending")
        self.assertFalse(second.paragraphs.exists())

    def test_a_requested_step_that_is_done_is_refused(self):
        self.failed_run()
        with self.assertRaises(services.StepsRefused):
            services.step_plan(self.run, ["equations", "paragraphs", "tables"])
        plan = services.step_plan(self.run, ["tables", "paragraphs"])
        self.assertEqual(plan["jobs"], ["paragraphs", "tables"])
        self.assertEqual(plan["kept"], ["equations", "figures"])


class StepStartHttpTests(Base):
    def setUp(self):
        super().setUp()
        key = ApiKey.objects.create(user=self.user, name="extraction")
        self.headers = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

    def post_start(self, body):
        return self.client.post(
            f"/extraction/v1/runs/{self.run.pk}/start", body, content_type="application/json", headers=self.headers
        )

    def test_start_with_steps(self):
        old = self.failed_run()
        body = {"page_count": 16, "page_sizes": SIZES, "jobs": ["paragraphs", "tables"], "steps": ["tables"]}
        response = self.post_start(body)
        self.assertEqual(response.status_code, 409, response.content[:400])
        self.assertEqual(response.json()["additional_data"], {"reason": "steps_refused"})

        response = self.post_start({**body, "steps": ["tables", "paragraphs"]})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.json()["response"]["attempt"], old + 1)

    def test_unknown_step_is_invalid(self):
        self.failed_run()
        body = {"page_count": 16, "page_sizes": SIZES, "jobs": ["outline"], "steps": ["outline"]}
        self.assertEqual(self.post_start(body).status_code, 422)


@override_settings(EVILFLOWERS_EXTRACTION_ENABLED=True, EVILFLOWERS_EXTRACTION_STEP_RERUN=True)
class StepJobTests(Base):
    def setUp(self):
        super().setUp()
        self.dispatcher = FakeDispatcher()
        patcher = mock.patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_empty_spec_is_the_spec_stored_before(self):
        self.assertEqual(ExtractionRunSpec().model_dump(mode="json"), {})
        spec = ExtractionRunSpec.model_validate({"steps": ["tables", "paragraphs", "tables"]})
        self.assertEqual(spec.model_dump(mode="json"), {"steps": ["paragraphs", "tables"]})

    @override_settings(EVILFLOWERS_EXTRACTION_STEP_RERUN=False)
    def test_the_gate_off_refuses(self):
        self.failed_run()
        with self.assertRaises(DispatchError) as raised:
            facade.apply(
                "extraction.run", self.acquisition.pk, spec={"steps": ["paragraphs", "tables"]}, user=self.user
            )
        self.assertEqual(raised.exception.status, 422)
        self.assertIn("EVILFLOWERS_EXTRACTION_STEP_RERUN", raised.exception.message)
        self.assertEqual(self.dispatcher.sent, [])

    @override_settings(EVILFLOWERS_EXTRACTION_ENABLED=False)
    def test_steps_are_refused_not_blocked_while_extraction_is_off(self):
        self.failed_run()
        with self.assertRaises(DispatchError) as raised:
            facade.apply(
                "extraction.run", self.acquisition.pk, spec={"steps": ["paragraphs", "tables"]}, user=self.user
            )
        self.assertEqual(raised.exception.status, 422)
        self.assertFalse(Job.objects.filter(kind="extraction.run").exists())

    def test_dry_run_lists_the_jobs_sent_again(self):
        self.failed_run()
        plan = facade.apply(
            "extraction.run",
            self.acquisition.pk,
            spec={"steps": ["paragraphs", "tables"]},
            user=self.user,
            dry_run=True,
        )
        self.assertEqual(plan.outcome, "would-run")
        self.assertEqual(plan.preview["resend"], ["paragraphs", "tables"])
        self.assertEqual(plan.preview["kept"], ["equations", "figures"])
        self.assertEqual(plan.preview["deleted"], {"paragraphs": 1, "tables": 1})
        self.assertEqual(self.dispatcher.sent, [])

    def test_unfinished_steps_left_out_are_refused(self):
        self.failed_run()
        with self.assertRaises(DispatchError) as raised:
            facade.apply("extraction.run", self.acquisition.pk, spec={"steps": ["paragraphs"]}, user=self.user)
        self.assertIn("tables", raised.exception.message)

    def test_apply_sends_the_steps_under_a_fresh_task_id_and_leaves_the_run_failed(self):
        self.failed_run()
        job = facade.apply(
            "extraction.run", self.acquisition.pk, spec={"steps": ["paragraphs", "tables"]}, user=self.user
        )
        (sent,) = self.dispatcher.sent
        self.assertEqual(sent["task"], "evilflowers_analyzer.extract")
        self.assertEqual(sent["kwargs"]["steps"], ["paragraphs", "tables"])
        self.assertEqual(sent["kwargs"]["run_id"], str(self.run.pk))
        self.assertNotEqual(sent["task_id"], services.task_id(self.acquisition.pk, self.run.pipeline_version))
        self.assertEqual(sent["task_id"], str(job.current_run_id))
        self.run.refresh_from_db()
        # Nothing is reset before the analyzer's step `/start`.
        self.assertEqual(self.run.status, ExtractionRun.Status.FAILED)
        self.assertEqual(self.run.paragraphs.count(), 1)

    def test_a_running_run_is_refused(self):
        services.start(self.run, page_count=1, page_sizes=SIZES[:1], jobs=["paragraphs"], provenance={})
        with self.assertRaises(DispatchError):
            facade.apply("extraction.run", self.acquisition.pk, spec={"steps": ["paragraphs"]}, user=self.user)

    def test_a_full_reapply_with_the_old_empty_spec_is_idempotent(self):
        with mock.patch("apps.extraction.services.queue", return_value=object()):
            job = facade.apply("extraction.run", self.acquisition.pk, user=self.user)
            Job.objects.filter(pk=job.pk).update(spec={})
            again = facade.apply("extraction.run", self.acquisition.pk, user=self.user)
        self.assertEqual(again.generation, job.generation)

    def test_observation_names_the_missing_steps(self):
        self.failed_run()
        job = facade.observe("extraction.run", self.acquisition)
        self.assertEqual(job.state, JobState.FAILED)
        self.assertEqual(job.detail["extraction_run"]["missing_steps"], ["paragraphs", "tables"])

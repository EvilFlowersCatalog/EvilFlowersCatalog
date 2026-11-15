"""#155: post-processing is recorded on the run, and a sweep catches every active run up by itself."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.assistant.tests import fixtures
from apps.core.models import Entry
from apps.extraction import tasks
from apps.extraction.models import ExtractionRun
from apps.extraction.tasks import POST_PROCESS_KIND, next_to_post_process
from apps.tasks.application.facade import facade
from apps.tasks.application.registry import registry
from apps.tasks.domain.values import DesiredState, JobState, RunStatus
from apps.tasks.models import Job, JobRun
from apps.tasks.tests.test_jobs import FakeDispatcher

STEPS = {
    "locators": "apps.extraction.locators.assign",
    "references": "apps.extraction.references.build",
    "regions": "apps.extraction.regions.index_run",
    "similarity": "apps.extraction.similarity.compute_run",
    "graph": "apps.graph.projection.project_run",
}


def bump_version(version):
    """`POST_PROCESS_VERSION` as a deploy that changed a step would set it."""
    return patch.multiple("apps.extraction.tasks", POST_PROCESS_VERSION=version), patch(
        "apps.extraction.jobs.POST_PROCESS_VERSION", version
    )


class PostProcessRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.entry, cls.extraction_run, *_ = fixtures.publication(cls.admin, cls.catalog)

    def steps(self, failing=()):
        patchers = [
            patch(target, side_effect=RuntimeError("down")) if name in failing else patch(target, return_value={})
            for name, target in STEPS.items()
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def marker(self):
        return ExtractionRun.objects.get(pk=self.extraction_run.pk).post_processed

    def test_marked_when_every_step_succeeded(self):
        self.steps()
        report = tasks.post_process_run(str(self.extraction_run.pk))
        self.assertEqual(report["version"], tasks.POST_PROCESS_VERSION)
        self.assertEqual(self.marker(), tasks.POST_PROCESS_VERSION)

    def test_a_failed_step_fails_the_run_and_leaves_no_marker(self):
        self.steps(failing=("regions",))
        with patch(STEPS["graph"], return_value={}) as graph:
            with self.assertRaisesRegex(RuntimeError, "failed in: regions"):
                tasks.post_process_run(str(self.extraction_run.pk))
        graph.assert_called_once()  # the remaining steps still ran
        self.assertIsNone(self.marker())

    def test_a_run_extracted_again_meanwhile_is_not_marked(self):
        def reextract(run):
            ExtractionRun.objects.filter(pk=run.pk).update(attempt=run.attempt + 1)
            return {}

        self.steps()
        with patch(STEPS["graph"], side_effect=reextract):
            tasks.post_process_run(str(self.extraction_run.pk))
        self.assertIsNone(self.marker())

    def test_activation_and_reset_clear_the_marker(self):
        from apps.extraction import services

        ExtractionRun.objects.filter(pk=self.extraction_run.pk).update(post_processed=1)
        run = ExtractionRun.objects.get(pk=self.extraction_run.pk)
        with patch("apps.extraction.services.delete_files"):
            services._reset(run)
        self.assertIsNone(self.marker())

        run.post_processed = 1
        run.jobs = {"text": ExtractionRun.JobStatus.DONE}
        with patch("apps.extraction.tasks.queue_post_process"), patch("apps.extraction.services.update_embedding"):
            services._finish(run)
        self.assertIsNone(run.post_processed)


class SweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.runs = {}
        for title, popularity in (("Popular", 50), ("Middle", 10), ("Quiet", 0)):
            entry, run, *_ = fixtures.publication(cls.admin, cls.catalog, title=title)
            Entry.objects.filter(pk=entry.pk).update(popularity=popularity)
            # Active long before the sweep existed: the backlog.
            ExtractionRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - timedelta(days=30))
            cls.runs[title] = run.pk

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        patcher = patch.object(facade, "dispatcher", self.dispatcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self):
        return [item["args"][0] for item in self.dispatcher.sent]

    def finish(self, status, mark=True):
        """Every queued run of the kind ends with `status`, as Celery events would report it."""
        for run in JobRun.objects.filter(kind=POST_PROCESS_KIND, status=RunStatus.QUEUED):
            if status is RunStatus.SUCCEEDED and mark:
                ExtractionRun.objects.filter(pk=run.subject_id).update(post_processed=tasks.POST_PROCESS_VERSION)
            run.status = status
            run.finished_at = timezone.now()
            run.save()
            facade.run_changed(run)

    def job(self, title):
        return Job.objects.get(kind=POST_PROCESS_KIND, subject_id=self.runs[title])

    # -- which runs are next ---------------------------------------------------------------------------------

    def test_unmarked_and_outdated_runs_most_read_first(self):
        ExtractionRun.objects.filter(pk=self.runs["Middle"]).update(post_processed=tasks.POST_PROCESS_VERSION)
        self.assertEqual(next_to_post_process(10), [self.runs["Popular"], self.runs["Quiet"]])
        ExtractionRun.objects.filter(pk=self.runs["Middle"]).update(post_processed=0)  # an older version
        self.assertEqual(next_to_post_process(10), [self.runs["Popular"], self.runs["Middle"], self.runs["Quiet"]])
        self.assertEqual(next_to_post_process(0), [])

    def test_inactive_runs_and_cancelled_jobs_are_left_alone(self):
        ExtractionRun.objects.filter(pk=self.runs["Quiet"]).update(is_active=False)
        tasks.post_process_sweep()
        Job.objects.filter(subject_id=self.runs["Popular"]).update(
            desired_state=DesiredState.CANCELLED, state=JobState.CANCELLED
        )
        self.finish(RunStatus.SUCCEEDED, mark=False)
        self.assertEqual(next_to_post_process(10), [self.runs["Middle"]])

    # -- the tick ----------------------------------------------------------------------------------------------

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=2)
    def test_each_tick_tops_up_to_the_batch(self):
        self.assertEqual(tasks.post_process_sweep()["declared"], 2)
        self.assertEqual(tasks.post_process_sweep()["declared"], 0)  # both in flight
        self.finish(RunStatus.SUCCEEDED)
        self.assertEqual(tasks.post_process_sweep()["runs"], [str(self.runs["Quiet"])])
        self.assertEqual(self.sent(), [str(self.runs["Popular"]), str(self.runs["Middle"]), str(self.runs["Quiet"])])
        self.finish(RunStatus.SUCCEEDED)
        self.assertEqual(tasks.post_process_sweep()["declared"], 0)  # caught up

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=2)
    def test_discovered_backlog_is_declared_and_summarised(self):
        self.assertEqual(facade.discover(POST_PROCESS_KIND), 3)
        self.assertEqual(
            set(Job.objects.filter(kind=POST_PROCESS_KIND).values_list("state", flat=True)), {JobState.QUEUED}
        )
        self.assertEqual(tasks.post_process_sweep()["declared"], 2)
        self.assertEqual(len(self.dispatcher.sent), 2)
        self.finish(RunStatus.SUCCEEDED)
        self.assertEqual(self.job("Popular").state, JobState.SUCCEEDED)
        # The daily re-read agrees: a marked run is a succeeded job.
        facade.discover(POST_PROCESS_KIND)
        self.assertEqual(self.job("Popular").state, JobState.SUCCEEDED)
        self.assertEqual(self.job("Popular").detail["post_process"]["marker"], tasks.POST_PROCESS_VERSION)

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=1, EVILFLOWERS_POST_PROCESS_MAX_ATTEMPTS=3)
    def test_a_failing_run_is_retried_then_skipped_until_applied(self):
        tasks.post_process_sweep()
        for attempt in (1, 2):
            self.finish(RunStatus.FAILED)  # TEI down
            self.assertEqual(self.job("Popular").state, JobState.PENDING)
            self.assertEqual(tasks.post_process_sweep()["declared"], 0)  # the retry counts as in flight
            facade.reconcile_due()
        self.finish(RunStatus.FAILED)
        self.assertEqual(self.job("Popular").state, JobState.FAILED)
        self.assertEqual(self.sent(), [str(self.runs["Popular"])] * 3)
        self.assertIsNone(ExtractionRun.objects.get(pk=self.runs["Popular"]).post_processed)

        # The sweep moves on, and the daily re-read does not revive the failed job.
        self.assertEqual(tasks.post_process_sweep()["runs"], [str(self.runs["Middle"])])
        facade.discover(POST_PROCESS_KIND)
        self.assertEqual(self.job("Popular").state, JobState.FAILED)
        self.assertNotIn(self.runs["Popular"], next_to_post_process(10))

        # A manual apply resets it.
        job = facade.apply(POST_PROCESS_KIND, self.runs["Popular"], check_permission=False)
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual(job.attempts, 1)

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=2)
    def test_a_new_version_requeues_every_run_a_batch_at_a_time(self):
        current = tasks.POST_PROCESS_VERSION
        ExtractionRun.objects.filter(pk__in=self.runs.values()).update(post_processed=current)
        facade.discover(POST_PROCESS_KIND)
        self.assertEqual(tasks.post_process_sweep()["declared"], 0)
        tasks_version, jobs_version = bump_version(current + 1)
        with tasks_version, jobs_version:
            facade.discover(POST_PROCESS_KIND)
            self.assertEqual(self.job("Quiet").state, JobState.QUEUED)
            self.assertEqual(tasks.post_process_sweep()["declared"], 2)
            self.finish(RunStatus.SUCCEEDED)
            self.assertEqual(tasks.post_process_sweep()["runs"], [str(self.runs["Quiet"])])
            self.assertEqual(ExtractionRun.objects.get(pk=self.runs["Popular"]).post_processed, current + 1)

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=1, EVILFLOWERS_POST_PROCESS_MAX_ATTEMPTS=1)
    def test_a_run_failed_for_an_older_version_is_swept_again(self):
        tasks.post_process_sweep()
        self.finish(RunStatus.FAILED)
        self.assertEqual(self.job("Popular").state, JobState.FAILED)
        self.assertNotIn(self.runs["Popular"], next_to_post_process(10))
        tasks_version, jobs_version = bump_version(tasks.POST_PROCESS_VERSION + 1)
        with tasks_version, jobs_version:
            self.assertEqual(next_to_post_process(1), [self.runs["Popular"]])

    @override_settings(EVILFLOWERS_POST_PROCESS_SWEEP_BATCH=1)
    def test_a_job_failed_before_the_sweep_existed_gets_a_round(self):
        # Attached to an activation's `.delay()` before #155: spec `{}`, one attempt, failed.
        facade.discover(POST_PROCESS_KIND)
        Job.objects.filter(subject_id=self.runs["Popular"]).update(state=JobState.FAILED, spec={}, attempts=1)
        facade.discover(POST_PROCESS_KIND)
        self.assertEqual(self.job("Popular").state, JobState.QUEUED)
        self.assertEqual(tasks.post_process_sweep()["runs"], [str(self.runs["Popular"])])
        self.assertEqual(self.job("Popular").spec, {"version": tasks.POST_PROCESS_VERSION})

    def test_a_run_published_by_the_activation_is_not_swept_twice(self):
        tasks.post_process_sweep()
        self.finish(RunStatus.SUCCEEDED)
        # Extracted again: the marker is cleared and `queue_post_process` published a run no worker has taken.
        ExtractionRun.objects.filter(pk=self.runs["Popular"]).update(post_processed=None)
        JobRun.objects.create(
            name="apps.extraction.tasks.post_process_run",
            kind=POST_PROCESS_KIND,
            subject_id=self.runs["Popular"],
            status=RunStatus.QUEUED,
            job=self.job("Popular"),
        )
        self.assertEqual(self.job("Popular").state, JobState.SUCCEEDED)
        self.dispatcher.sent.clear()
        tasks.post_process_sweep()
        self.assertNotIn(str(self.runs["Popular"]), self.sent())


class KindsTests(TestCase):
    def test_the_sweeps_are_maintenance_kinds(self):
        for kind, task in (
            ("maintenance.post_process_sweep", "apps.extraction.tasks.post_process_sweep"),
            ("maintenance.similarity_rebuild", "apps.extraction.tasks.similarity_rebuild"),
        ):
            definition = registry.get(kind)
            self.assertIsNotNone(definition, kind)
            self.assertEqual(definition.task, task)
        with override_settings(EVILFLOWERS_POST_PROCESS_MAX_ATTEMPTS=5):
            self.assertEqual(registry.get(POST_PROCESS_KIND).max_attempts, 5)

    def test_the_similarity_rebuild_runs_the_whole_library(self):
        with patch("apps.extraction.similarity.rebuild", return_value=1234):
            self.assertEqual(tasks.similarity_rebuild(), {"pairs": 1234})

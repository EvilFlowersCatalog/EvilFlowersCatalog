"""#192: the author list pattern backtracked for an hour; post-processing ran on, and came back, regardless."""

import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.assistant.tests import fixtures
from apps.extraction import bibliography, tasks
from apps.extraction.models import ExtractionRun
from apps.tasks.domain.values import DesiredState, JobState, RunStatus
from apps.tasks.models import Job, JobRun

STEPS = (
    "apps.extraction.bibliography.build",
    "apps.extraction.locators.assign",
    "apps.extraction.references.build",
    "apps.extraction.regions.index_run",
    "apps.extraction.similarity.compute_run",
    "apps.graph.projection.project_run",
)


class AuthorListTests(SimpleTestCase):
    #: Before #192, eight authors without commas followed by a year took more than 20 s, sixteen hours.
    NEAR_MISSES = (
        "Kates, R. W. " * 15 + "Kates, R. W. 1999",
        "Kates, R.-W. " * 15 + "Kates, R.-W. 1999",
        "W. C. Clark, " * 20 + "W. C. Clark in",
        "Barro RJ " * 30 + "x",
    )

    def test_near_misses_fail_fast(self):
        for tail in self.NEAR_MISSES:
            with self.subTest(tail=tail[:40]):
                started = time.perf_counter()
                self.assertIsNone(bibliography.AUTHOR_LIST.match(tail))
                self.assertLess(time.perf_counter() - started, 0.05)

    def test_author_lists_still_match(self):
        for tail in (
            "Kates, R. W.",
            "Kates, R. W., Clark, W. C., & Smith, J.",
            "Kates, R. W., W. C. Clark, and S. Collins",
            "Kates RW, Clark WC",
            "van Dijk, T. A.; de la Cruz, J.-P.",
            "Müller, K. und Šťastný, Ž. (Hrsg.)",
            "Kates, R. W. Clark, W. C.",  # the comma lost between two authors
            "Barro RJ Kates RW (eds)",
            "Kates, R. W., et al.",
            "Gurdon JB.",
        ):
            with self.subTest(tail=tail):
                self.assertIsNotNone(bibliography.AUTHOR_LIST.match(tail))

    def test_a_long_stretch_of_years_without_authors_stays_fast(self):
        # Years the splitter cannot attribute did not move the window's start: it grew with every one of them.
        prose = " ".join(
            f"Kates, R. W. Kates, R. W. Kates, R. W. Kates, R. W. noted in ({1950 + i})" for i in range(60)
        )
        text = prose + " Smith, J. (2001). A title. Journal 3, 1–2. Brown, K. (2002). Another. Press."
        started = time.perf_counter()
        items = bibliography.split(text)
        self.assertLess(time.perf_counter() - started, 2)
        self.assertEqual([item.first_author for item in items][-2:], ["Smith, J", "Brown, K"])


#: Dev, run a1fae3d1 (The Early Years), paragraphs 1124-1131 of its references: Chicago author lists (`Berry, D.,
#: C. Blair, A. Ursache, …`). The old pattern took 2.2 s on these 1.9 KB; the whole section held the worker 1 h.
EARLY_YEARS = " ".join(
    [
        "equidad y movilidad social en Colombia.” CEDE Working Paper No. 33. Centro de Estudios sobre Desarrollo "
        "Econ ó mico, Universidad de los Andes, Bogot á . Bernal, R., and C. Fern á ndez. 2013. “Subsidized "
        "Childcare and Child Development in",
        "Colombia: Effects of Hogares Comunitarios de Bienestar as a Function of Timing and Length of Exposure.” "
        "Social Science and Medicine 97(2013) November: 241–49. Bernal, R., C. Fern á ndez, C. E. Fl ó rez, "
        "A. Gaviria, P. R. Ocampo, B. Samper, and F. S á nchez.",
        "2009. “Evaluaci ó n de impacto del Programa Hogares Comunitarios de Bienestar del ICBF.” CEDE Working "
        "Paper No. 16. Centro de Estudios sobre Desarrollo Econ ó mico, Universidad de los Andes, Bogot á . "
        "Bernal, R., and M. P. Keane. 2011. “Child Care Choices and Children’s Cognitive Achievement:",
        "The Case of Single Mothers.” Journal of Labor Economics 29(3) July: 459–512. Berry, D., C. Blair, "
        "A. Ursache, M. Willoughby, P. Garrett-Peters, L. Vernon-Feagans,",
        "M. Bratsch-Hines, W. R. Mills-Koonce, D. A. Granger, and Family Life Project Key Investigators. 2014. "
        "“Child Care and Cortisol across Early Childhood: Context Matters.” Developmental Psychology 50(2) "
        "February: 514–25. Beuermann, D. W., J. Cristi á , S. Cueto, O. Malamud, and Y. Cruz-Aguayo. 2015. “One",
        "Laptop per Child at Home: Short-Term Impacts from a Randomized Experiment in Peru.” American Economic "
        "Journal: Applied Economics 7(2) April: 53–80. Bhalotra, S. 2010. “Fatal Fluctuations? Cyclicality in "
        "Infant Mortality in India.” Journal of",
        "Development Economics 93(1) September: 7–19. Bhandari, N., R. Bahl, S. Mazumdar, J. Martines, R. E. "
        "Black, M. K. Bhan, and the Infant",
        "Feeding Study Group. 2003. “Effect of Community-Based Promotion of Exclusive Breastfeeding on Diarrhoeal "
        "Illness and Growth: A Cluster Randomised Controlled Trial.” Lancet 361(9367) April: 1418–23. Bharadwaj, "
        "P., J. Eberhard, and C. Neilson. 2014. “Health at Birth, Parental Investments",
    ]
)


class RealReferencesTests(SimpleTestCase):
    def test_the_early_years_splits_fast_and_as_before(self):
        started = time.perf_counter()
        items = bibliography.split(EARLY_YEARS)
        self.assertLess(time.perf_counter() - started, 0.2)
        # The same items the old pattern found, in 2.2 s.
        self.assertEqual(
            [(item.start, item.year) for item in items],
            [(697, 2011), (997, 2014), (1203, 2015), (1404, 2010), (1611, 2003), (1830, 2014)],
        )


class PostProcessGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.entry, cls.extraction_run, *_ = fixtures.publication(cls.admin, cls.catalog)

    def setUp(self):
        self.patchers = [patch(target, return_value={}) for target in STEPS]
        self.mocks = [patcher.start() for patcher in self.patchers]
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)

    def job(self, state=JobState.RUNNING, desired=DesiredState.COMPLETED) -> Job:
        return Job.objects.create(
            kind=tasks.POST_PROCESS_KIND,
            subject_type=ContentType.objects.get_for_model(ExtractionRun),
            subject_id=self.extraction_run.pk,
            spec={"version": tasks.POST_PROCESS_VERSION},
            state=state,
            desired_state=desired,
        )

    def run_of(self, job, created_at=None) -> JobRun:
        run = JobRun.objects.create(
            id=uuid.uuid4(),
            name="apps.extraction.tasks.post_process_run",
            kind=tasks.POST_PROCESS_KIND,
            subject_id=self.extraction_run.pk,
            status=RunStatus.LOST,
            job=job,
        )
        if created_at is not None:
            JobRun.objects.filter(pk=run.pk).update(created_at=created_at)
            run.refresh_from_db()
        return run

    def deliver(self, run):
        return tasks.post_process_run.apply(args=[str(self.extraction_run.pk)], task_id=str(run.pk)).get()

    def test_a_redelivery_after_the_job_failed_does_nothing(self):
        job = self.job(state=JobState.FAILED)
        run = self.run_of(job)
        Job.objects.filter(pk=job.pk).update(current_run=run)
        self.assertEqual(self.deliver(run), {"skipped": "job failed"})
        for mock in self.mocks:
            mock.assert_not_called()

    def test_a_new_run_of_a_failed_job_runs_before_the_job_moves(self):
        # The publisher moves the job to `queued` on commit; the worker may be quicker.
        job = self.job(state=JobState.FAILED)
        failed = self.run_of(job, created_at=timezone.now() - timedelta(hours=1))
        Job.objects.filter(pk=job.pk).update(current_run=failed)
        fresh = self.run_of(job)
        self.assertEqual(self.deliver(fresh)["version"], tasks.POST_PROCESS_VERSION)

    def test_a_redelivery_of_a_replaced_run_does_nothing(self):
        job = self.job()
        old = self.run_of(job, created_at=timezone.now() - timedelta(hours=12))
        new = self.run_of(job)
        Job.objects.filter(pk=job.pk).update(current_run=new)
        self.assertEqual(self.deliver(old), {"skipped": f"replaced by run {new.pk}"})

    def test_a_cancelled_job_does_nothing(self):
        job = self.job(desired=DesiredState.CANCELLED)
        run = self.run_of(job)
        self.assertEqual(self.deliver(run), {"skipped": "job cancelled"})

    def test_the_current_run_and_an_unknown_task_run(self):
        job = self.job()
        run = self.run_of(job)
        Job.objects.filter(pk=job.pk).update(current_run=run)
        self.assertEqual(self.deliver(run)["version"], tasks.POST_PROCESS_VERSION)
        ExtractionRun.objects.filter(pk=self.extraction_run.pk).update(post_processed=None)
        unknown = tasks.post_process_run.apply(args=[str(self.extraction_run.pk)], task_id=str(uuid.uuid4())).get()
        self.assertEqual(unknown["version"], tasks.POST_PROCESS_VERSION)

    def test_the_soft_time_limit_fails_the_run_and_skips_the_rest(self):
        self.mocks[0].side_effect = SoftTimeLimitExceeded()
        with self.assertRaisesRegex(RuntimeError, "in step bibliography"):
            tasks.post_process_run(str(self.extraction_run.pk))
        for mock in self.mocks[1:]:
            mock.assert_not_called()
        self.assertIsNone(ExtractionRun.objects.get(pk=self.extraction_run.pk).post_processed)

    def test_the_task_and_the_job_kind_carry_the_limits(self):
        from apps.tasks.application.registry import registry

        self.assertEqual(tasks.post_process_run.soft_time_limit, tasks.POST_PROCESS_SOFT_TIME_LIMIT)
        self.assertEqual(tasks.post_process_run.time_limit, tasks.POST_PROCESS_TIME_LIMIT)
        self.assertEqual(
            registry.get(tasks.POST_PROCESS_KIND).time_limit, timedelta(seconds=tasks.POST_PROCESS_TIME_LIMIT)
        )

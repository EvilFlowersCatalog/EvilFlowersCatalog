"""Ingestion survives an analyzer pass done twice (#120), and a run nobody reports to is failed."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.extraction import services
from apps.extraction.models import ExtractionRun, Paragraph, Region

BOX = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}


def paragraph(seq: int) -> dict:
    return {"page": 1, "seq": seq, "bbox": BOX, "text": f"Paragraph {seq}.", "sentences": []}


class IngestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        owner = User(username="owner", name="O", surname="W", auth_source=source)
        owner.set_unusable_password()
        owner.save()
        catalog = Catalog.objects.create(creator=owner, title="Open", url_name="open", is_public=True)
        entry = Entry.objects.create(creator=owner, catalog=catalog, title="Book")
        cls.acquisition = Acquisition.objects.create(
            entry=entry, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/open/book.pdf"
        )

    def setUp(self):
        self.run = ExtractionRun.objects.create(acquisition=self.acquisition, pipeline_version=1)

    def start(self, jobs=("paragraphs", "figures")):
        return services.start(self.run, page_count=1, page_sizes=[[595, 842]], jobs=jobs, provenance={})

    def test_second_start_drops_results_of_the_first_pass(self):
        self.start()
        services.add_paragraphs(self.run, [paragraph(0), paragraph(1)], [])
        Region.objects.create(run=self.run, kind=Region.Kind.FIGURE, page=1, seq=0, bbox=BOX)
        services.report_job(self.run, "paragraphs", failed=False, error=None, provenance={})

        run = self.start()

        self.assertEqual(run.status, ExtractionRun.Status.RUNNING)
        self.assertEqual(run.jobs, {"paragraphs": "pending", "figures": "pending"})
        self.assertFalse(Paragraph.objects.filter(run=run).exists())
        self.assertFalse(Region.objects.filter(run=run).exists())
        # The second pass stores its paragraphs from seq 0 again without colliding.
        self.assertEqual(services.add_paragraphs(run, [paragraph(0)], []), 1)

    def test_every_start_and_reset_is_a_new_attempt(self):
        self.assertEqual(self.start().attempt, 1)
        self.assertEqual(self.start().attempt, 2)
        services._reset(self.run)
        self.run.refresh_from_db()
        self.assertEqual(self.run.attempt, 3)
        self.assertEqual(self.start().attempt, 4)

    def test_results_of_an_earlier_attempt_are_superseded(self):
        first = self.start().attempt
        second = self.start().attempt

        with self.assertRaises(services.RunSuperseded) as raised:
            services.add_paragraphs(self.run, [paragraph(0)], [], attempt=first)
        self.assertEqual((raised.exception.attempt, raised.exception.current), (first, second))
        with self.assertRaises(services.RunSuperseded):
            services.add_regions(self.run, [], attempt=first)
        with self.assertRaises(services.RunSuperseded):
            services.report_job(self.run, "paragraphs", failed=False, error=None, provenance={}, attempt=first)
        with self.assertRaises(services.RunSuperseded):
            services.check_attempt(self.run, first)

        self.run.refresh_from_db()
        self.assertEqual(self.run.jobs, {"paragraphs": "pending", "figures": "pending"})
        self.assertFalse(self.run.paragraphs.exists())
        # The current attempt, and services that send none, are accepted.
        self.assertEqual(services.add_paragraphs(self.run, [paragraph(0)], [], attempt=second), 1)
        self.assertEqual(services.add_paragraphs(self.run, [paragraph(1)], []), 1)
        services.check_attempt(self.run, second)

    def test_an_earlier_attempt_after_a_reset_is_superseded_not_just_not_running(self):
        attempt = self.start().attempt
        services._reset(self.run)
        self.run.refresh_from_db()
        with self.assertRaises(services.RunSuperseded):
            services.add_paragraphs(self.run, [paragraph(0)], [], attempt=attempt)
        # Without an attempt the old answer stands.
        with self.assertRaises(services.RunNotRunning) as raised:
            services.add_paragraphs(self.run, [paragraph(0)], [])
        self.assertNotIsInstance(raised.exception, services.RunSuperseded)

    def test_a_superseded_report_is_counted_and_changes_nothing(self):
        attempt = self.start().attempt
        self.start()
        with mock.patch("evil_flowers_catalog.domain_metrics.extraction_jobs_superseded") as counter:
            services.report_superseded(self.run, "paragraphs", attempt)
        counter.add.assert_called_once_with(
            1, {"evilflowers.extraction.job": "paragraphs", "evilflowers.extraction.superseded.stage": "early"}
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.RUNNING)
        self.assertEqual(self.run.jobs, {"paragraphs": "pending", "figures": "pending"})

    def test_start_of_a_finished_run_is_refused(self):
        self.run.status = ExtractionRun.Status.DONE
        self.run.save()
        with self.assertRaises(services.RunNotRunning):
            self.start()

    def test_duplicate_seq_is_a_conflict_not_a_crash(self):
        self.start()
        services.add_paragraphs(self.run, [paragraph(0)], [])
        with self.assertRaises(services.DuplicateParagraphs):
            services.add_paragraphs(self.run, [paragraph(0), paragraph(1)], [])
        # The failed batch left nothing behind.
        self.assertEqual(list(self.run.paragraphs.values_list("seq", flat=True)), [0])

    def test_failure_of_a_job_already_done_is_ignored(self):
        self.start()
        services.report_job(self.run, "paragraphs", failed=False, error=None, provenance={})
        services.report_job(self.run, "paragraphs", failed=True, error="duplicate", provenance={})

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.RUNNING)
        self.assertEqual(self.run.jobs["paragraphs"], "done")
        self.assertIsNone(self.run.error)

    def idle_for(self, hours: float) -> None:
        # `updated_at` is auto_now: move it back with a queryset update.
        ExtractionRun.objects.filter(pk=self.run.pk).update(updated_at=timezone.now() - timedelta(hours=hours))

    def test_a_run_nobody_reports_to_is_failed_and_says_what_was_pending(self):
        self.start(jobs=("paragraphs", "tables", "equations"))
        services.report_job(self.run, "paragraphs", failed=False, error=None, provenance={})
        ExtractionRun.objects.filter(pk=self.run.pk).update(error="figures: earlier")
        self.idle_for(25)

        self.assertEqual(services.fail_stale_runs(24), 1)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.FAILED)
        self.assertEqual(self.run.error, "figures: earlier\nno report for 24 h, pending: equations, tables")
        self.assertIsNotNone(self.run.finished_at)
        # A failed run is what `queue` retries.
        with mock.patch.object(services, "_send"), self.settings(EVILFLOWERS_EXTRACTION_ENABLED=True):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(services.queue(self.acquisition).status, ExtractionRun.Status.QUEUED)

    def test_a_run_with_a_recent_report_is_left_alone(self):
        self.start()
        self.idle_for(23)
        self.assertEqual(services.fail_stale_runs(24), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.RUNNING)

    def test_only_running_runs_are_swept(self):
        # Queued runs wait on the analyzer's backlog; they are never started, so never stale here.
        self.idle_for(100)
        self.assertEqual(services.fail_stale_runs(24), 0)


class AttemptContractTests(TestCase):
    """#140 over HTTP: what the services see."""

    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        service = User(username="extraction", name="E", surname="S", auth_source=source)
        service.set_unusable_password()
        service.save()
        service.user_permissions.add(Permission.objects.get(codename="ingest_extraction"))
        catalog = Catalog.objects.create(creator=service, title="Open", url_name="open", is_public=True)
        entry = Entry.objects.create(creator=service, catalog=catalog, title="Book")
        cls.acquisition = Acquisition.objects.create(
            entry=entry, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/open/book.pdf"
        )
        key = ApiKey.objects.create(user=service, name="extraction")
        cls.headers = {"Authorization": f"Bearer {JWTFactory(service.pk).api_key(str(key.pk))}"}

    def setUp(self):
        self.run = ExtractionRun.objects.create(acquisition=self.acquisition, pipeline_version=1)
        self.base = f"/extraction/v1/runs/{self.run.pk}"

    def post(self, path, body, attempt=None):
        url = f"{self.base}{path}" + ("" if attempt is None else f"?attempt={attempt}")
        return self.client.post(url, body, content_type="application/json", headers=self.headers)

    def start(self) -> int:
        response = self.post("/start", {"page_count": 1, "page_sizes": [[595, 842]], "jobs": ["paragraphs"]})
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()["response"]["attempt"]

    def test_start_and_the_run_carry_the_attempt(self):
        self.assertEqual(self.start(), 1)
        self.assertEqual(self.start(), 2)
        response = self.client.get(self.base, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.json()["response"]["attempt"], 2)

    def test_results_of_an_earlier_attempt_are_409_superseded(self):
        stale = self.start()
        current = self.start()
        with mock.patch("evil_flowers_catalog.domain_metrics.extraction_jobs_superseded") as counter:
            response = self.post("/paragraphs", {"paragraphs": [paragraph(0)]}, attempt=stale)
        self.assertEqual(response.status_code, 409, response.content[:400])
        self.assertEqual(response.json()["additional_data"], {"reason": "superseded", "attempt": current})
        counter.add.assert_called_once_with(
            1, {"evilflowers.extraction.job": "paragraphs", "evilflowers.extraction.superseded.stage": "late"}
        )
        self.assertFalse(self.run.paragraphs.exists())

        response = self.post("/paragraphs", {"paragraphs": [paragraph(0)]}, attempt=current)
        self.assertEqual(response.status_code, 201, response.content[:400])

    def test_a_run_not_running_says_so(self):
        response = self.post("/paragraphs", {"paragraphs": [paragraph(0)]})
        self.assertEqual(response.status_code, 409, response.content[:400])
        self.assertEqual(response.json()["additional_data"], {"reason": "not_running"})

    def test_an_invalid_attempt_is_400(self):
        self.start()
        response = self.post("/paragraphs", {"paragraphs": [paragraph(0)]}, attempt="abc")
        self.assertEqual(response.status_code, 400, response.content[:400])

    def test_a_superseded_report_is_counted_and_leaves_the_run_alone(self):
        stale = self.start()
        self.start()
        with mock.patch("evil_flowers_catalog.domain_metrics.extraction_jobs_superseded") as counter:
            response = self.post("/jobs/paragraphs", {"status": "superseded"}, attempt=stale)
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.json()["response"]["jobs"], {"paragraphs": "pending"})
        counter.add.assert_called_once_with(
            1, {"evilflowers.extraction.job": "paragraphs", "evilflowers.extraction.superseded.stage": "early"}
        )

    def test_a_stale_analyzer_failure_does_not_fail_the_new_attempt(self):
        stale = self.start()
        self.start()
        response = self.post("/jobs/analyzer", {"status": "failed", "error": "old pass"}, attempt=stale)
        self.assertEqual(response.status_code, 409, response.content[:400])
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.RUNNING)

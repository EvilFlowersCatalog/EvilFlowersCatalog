"""Ingestion survives an analyzer pass done twice (#120)."""

from django.test import TestCase

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
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

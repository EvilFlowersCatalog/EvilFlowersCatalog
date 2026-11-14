"""Runs with hundreds of per-batch jobs (#150): grouped provenance, a short stale reason, a grouped job list."""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.extraction import batches, services
from apps.extraction.models import ExtractionRun

PAGES = 928
BATCH = 8
KINDS = ("figures", "equations", "tables")


def batch_jobs(pages: int = PAGES, size: int = BATCH) -> list[str]:
    """What the analyzer declares with `DISPATCH_PER_BATCH`: one job per batch per kind, plus paragraphs."""
    ranges = [(start, min(start + size - 1, pages)) for start in range(1, pages + 1, size)]
    return [f"{kind}:{low:04d}-{high:04d}" for kind in KINDS for low, high in ranges] + ["paragraphs"]


class JobNameTests(SimpleTestCase):
    def test_kind_and_pages(self):
        self.assertEqual(batches.job_kind("equations:0297-0304"), "equations")
        self.assertEqual(batches.job_kind("paragraphs"), "paragraphs")
        self.assertEqual(batches.batch_pages("equations:0297-0304"), (297, 304))
        self.assertIsNone(batches.batch_pages("paragraphs"))
        self.assertIsNone(batches.batch_pages("figures:retry"))

    def test_group_counts_and_pending_span(self):
        jobs = dict.fromkeys(batch_jobs(), "done")
        jobs["equations:0297-0304"] = "pending"
        jobs["equations:0921-0928"] = "pending"
        jobs["figures:0001-0008"] = "failed"
        groups = {group.kind: group.as_dict() for group in batches.group_jobs(jobs)}
        self.assertEqual(list(groups), ["equations", "figures", "paragraphs", "tables"])
        self.assertEqual(
            groups["equations"],
            {"kind": "equations", "total": 116, "done": 114, "pending": 2, "failed": 0, "pending_pages": [297, 928]},
        )
        self.assertEqual((groups["figures"]["failed"], groups["figures"]["pending_pages"]), (1, None))
        self.assertEqual(groups["paragraphs"]["total"], 1)

    def test_pending_summary_is_short(self):
        pending = [f"equations:{low:04d}-{low + 7:04d}" for low in range(297, 928, 8)] + [
            "figures:0001-0008",
            "figures:0009-0016",
            "paragraphs",
        ]
        self.assertEqual(
            batches.pending_summary(pending),
            "equations ×79 (pages 297–928), figures ×2 (pages 1–16), paragraphs",
        )
        self.assertEqual(batches.pending_summary([]), "none")
        self.assertEqual(batches.pending_summary(["figures:0009-0016"]), "figures ×1 (pages 9–16)")

    def test_pending_summary_caps_the_kinds(self):
        names = [f"kind{index}:0001-0008" for index in range(9)]
        self.assertTrue(batches.pending_summary(names).endswith("+3 more kinds"))

    def test_provenance_merges_batches_per_kind(self):
        model = {"image": "crop", "latex_model": "texify@1"}
        provenance = {}
        for job in batch_jobs():
            if job.startswith("equations:"):
                provenance = batches.merge_provenance(provenance, job, model)
        self.assertEqual(provenance, {"equations": model})

        swapped = {"image": "crop", "latex_model": "texify@2"}
        provenance = batches.merge_provenance(provenance, "equations:0921-0928", swapped)
        provenance = batches.merge_provenance(provenance, "equations:0913-0920", swapped)
        self.assertEqual(provenance["equations"], {**model, "variants": [swapped]})
        # A batch matching the base after a variant changes nothing.
        self.assertEqual(batches.merge_provenance(provenance, "equations:0001-0008", model), provenance)

    def test_variants_are_capped(self):
        provenance = {"figures": {"model": "a"}}
        for index in range(batches.MAX_VARIANTS + 5):
            provenance = batches.merge_provenance(provenance, f"figures:{index:04d}-{index:04d}", {"model": index})
        self.assertEqual(len(provenance["figures"]["variants"]), batches.MAX_VARIANTS)

    def test_unbatched_job_replaces_its_entry(self):
        provenance = batches.merge_provenance(
            {"paragraphs": {"embedding_model": "e5@1"}}, "paragraphs", {"embedding_model": "e5@2"}
        )
        self.assertEqual(provenance, {"paragraphs": {"embedding_model": "e5@2"}})


class PerBatchRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.admin = User.objects.create(username="admin", name="A", surname="D", auth_source=source, is_superuser=True)
        catalog = Catalog.objects.create(creator=cls.admin, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(creator=cls.admin, catalog=catalog, title="Book")
        cls.acquisition = Acquisition.objects.create(
            entry=cls.entry, mime=Acquisition.AcquisitionMIME.PDF, content="catalogs/open/book.pdf"
        )

    def setUp(self):
        self.jobs = batch_jobs()
        self.assertEqual(len(self.jobs), 349)
        self.run = ExtractionRun.objects.create(acquisition=self.acquisition, pipeline_version=1)
        services.start(self.run, page_count=PAGES, page_sizes=[[595, 842]] * PAGES, jobs=self.jobs, provenance={})

    def report_all_but(self, pending: set[str]):
        for job in self.jobs:
            if job in pending:
                continue
            kind = batches.job_kind(job)
            provenance = {"embedding_model": "e5@1"} if kind == "paragraphs" else {"model": f"{kind}@1"}
            services.report_job(self.run, job, failed=False, error=None, provenance=provenance)

    def test_provenance_has_one_entry_per_kind(self):
        self.report_all_but(set())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.DONE)
        self.assertEqual(
            self.run.provenance,
            {
                "equations": {"model": "equations@1"},
                "figures": {"model": "figures@1"},
                "paragraphs": {"embedding_model": "e5@1"},
                "tables": {"model": "tables@1"},
            },
        )

    def test_stale_reason_is_summarised(self):
        pending = {job for job in self.jobs if job.startswith("equations:") and job >= "equations:0297"}
        pending |= {"figures:0001-0008", "figures:0009-0016"}
        self.report_all_but(pending)
        ExtractionRun.objects.filter(pk=self.run.pk).update(updated_at=timezone.now() - timedelta(hours=7))

        self.assertEqual(services.fail_stale_runs(6), 1)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ExtractionRun.Status.FAILED)
        self.assertEqual(
            self.run.error, "no report for 6 h, pending: equations ×79 (pages 297–928), figures ×2 (pages 1–16)"
        )

    def test_admin_lists_jobs_grouped_by_kind(self):
        self.report_all_but({"tables:0921-0928"})
        key = ApiKey.objects.create(user=self.admin, name="test")
        headers = {"Authorization": f"Bearer {JWTFactory(self.admin.pk).api_key(str(key.pk))}"}
        with mock.patch("apps.extraction.admin_views._broker_client", return_value=None):
            response = self.client.get(f"/api/v1/entries/{self.entry.pk}/extraction", headers=headers)
        self.assertEqual(response.status_code, 200, response.content)
        run = response.json()["response"]["acquisitions"][0]["runs"][0]
        self.assertEqual(
            run["job_groups"],
            [
                {"kind": "equations", "total": 116, "done": 116, "pending": 0, "failed": 0, "pending_pages": None},
                {"kind": "figures", "total": 116, "done": 116, "pending": 0, "failed": 0, "pending_pages": None},
                {"kind": "paragraphs", "total": 1, "done": 1, "pending": 0, "failed": 0, "pending_pages": None},
                {"kind": "tables", "total": 116, "done": 115, "pending": 1, "failed": 0, "pending_pages": [921, 928]},
            ],
        )


class MetricLabelTests(SimpleTestCase):
    def test_superseded_counter_labels_by_kind(self):
        from evil_flowers_catalog import domain_metrics

        with mock.patch.object(domain_metrics, "extraction_jobs_superseded") as counter:
            domain_metrics.record_extraction_superseded("equations:0297-0304", "late")
        counter.add.assert_called_once_with(
            1, {"evilflowers.extraction.job": "equations", "evilflowers.extraction.superseded.stage": "late"}
        )

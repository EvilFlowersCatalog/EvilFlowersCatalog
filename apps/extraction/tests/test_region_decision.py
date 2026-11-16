"""#189: decider bands on extraction regions — stored as sent, hidden from readers, re-banded from stored p."""

import io
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import figures as describe
from apps.assistant.tests import fixtures
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.extraction import regions, services
from apps.extraction.bands import band_for
from apps.extraction.models import ExtractionRun, Region
from apps.extraction.tests.test_figure_search import QUERY, FakeStorage, FigureFixture

BANDS = {
    "figure": {"hide": ["stamp", "decoration", "blank"], "upper": 0.9, "lower": 0.6},
    "table": {"upper": 0.7, "lower": 0.3},
}
BBOX = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5}


def region(kind="figure", seq=0, **extra) -> dict:
    return {"kind": kind, "page": 1, "seq": seq, "bbox": BBOX, "image_path": f"r{seq}.webp", **extra}


class BandForTests(SimpleTestCase):
    def test_no_thresholds_bands_nothing(self):
        self.assertIsNone(band_for("figure", "stamp", 0.99, config={}))
        self.assertIsNone(band_for("table", None, 0.1, 0.1, config={"table": {"upper": 0.7}}))

    def test_figure(self):
        self.assertEqual(band_for("figure", "stamp", 0.95, config=BANDS), "hide")
        self.assertEqual(band_for("figure", "stamp", 0.7, config=BANDS), "review")
        self.assertEqual(band_for("figure", "stamp", 0.2, config=BANDS), "keep")
        self.assertEqual(band_for("figure", "chart", 0.99, config=BANDS), "keep")
        self.assertIsNone(band_for("figure", None, 0.99, config=BANDS))

    def test_table_prefers_the_score(self):
        self.assertEqual(band_for("table", None, 0.9, config=BANDS), "keep")
        self.assertEqual(band_for("table", None, 0.9, 0.1, config=BANDS), "hide")
        self.assertEqual(band_for("table", None, None, 0.5, config=BANDS), "review")
        self.assertEqual(band_for("table", None, 0.0, config=BANDS), "hide")

    def test_equations_are_never_banded(self):
        self.assertIsNone(band_for("equation", "x", 0.99, config={"equation": {"upper": 0.5, "lower": 0.1}}))


class IngestDecisionTests(TestCase):
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
        response = self.post("/start", {"page_count": 1, "page_sizes": [[595, 842]], "jobs": ["figures", "tables"]})
        self.assertEqual(response.status_code, 200, response.content[:400])

    def post(self, path, body):
        return self.client.post(f"{self.base}{path}", body, content_type="application/json", headers=self.headers)

    def regions(self, *items):
        response = self.post("/regions", {"regions": list(items)})
        self.assertEqual(response.status_code, 201, response.content[:400])
        return {row.seq: row for row in self.run.regions.all()}

    def test_the_decision_is_stored_and_zero_is_a_probability(self):
        rows = self.regions(
            region(seq=0, band="hide", p=0.97, decision="stamp", decider_model="strands-decider-2B@216d1ccb"),
            region(
                "table",
                seq=1,
                band="review",
                p=0.0,
                score=0.42,
                decider_model="strands-decider-2B@216d1ccb",
                review_outcome="rewritten",
                grounded_share=0.0,
                signals={"filled_share": 0.8, "strategy": "lines"},
            ),
        )
        stamp, table = rows[0], rows[1]
        self.assertEqual((stamp.band, stamp.decision_p, stamp.decision), ("hide", 0.97, "stamp"))
        self.assertEqual(stamp.decider_model, "strands-decider-2B@216d1ccb")
        self.assertEqual((table.band, table.decision_p, table.decision_score), ("review", 0.0, 0.42))
        self.assertEqual(
            table.decision_signals,
            {"filled_share": 0.8, "strategy": "lines", "review_outcome": "rewritten", "grounded_share": 0.0},
        )
        self.run.refresh_from_db()
        self.assertEqual(
            self.run.provenance[services.DECIDER_MODELS],
            {"figures": ["strands-decider-2B@216d1ccb"], "tables": ["strands-decider-2B@216d1ccb"]},
        )

    def test_without_a_decision_the_region_is_unknown(self):
        row = self.regions(region())[0]
        self.assertEqual((row.band, row.decision_p, row.decision, row.decider_model), (None, None, None, None))
        self.assertEqual(row.decision_signals, {})
        self.run.refresh_from_db()
        self.assertNotIn(services.DECIDER_MODELS, self.run.provenance)

    def test_bad_values_are_refused(self):
        for bad in ({"band": "maybe"}, {"p": 1.5}, {"signals": [1, 2]}):
            response = self.post("/regions", {"regions": [region(**bad)]})
            self.assertEqual(response.status_code, 422, bad)
        self.assertFalse(self.run.regions.exists())

    def test_the_catalog_bands_only_with_thresholds(self):
        self.assertIsNone(self.regions(region(seq=0, p=0.95, decision="stamp"))[0].band)
        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS):
            rows = self.regions(
                region(seq=1, p=0.95, decision="stamp"), region(seq=2, band="keep", p=0.95, decision="stamp")
            )
        # Computed when the worker sent none; the worker's band wins otherwise.
        self.assertEqual((rows[1].band, rows[2].band), ("hide", "keep"))

    def test_a_new_decider_model_is_added_once(self):
        self.regions(region(seq=0, decider_model="a@1"))
        self.regions(region(seq=1, decider_model="a@1"), region(seq=2, decider_model="b@2"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.provenance[services.DECIDER_MODELS], {"figures": ["a@1", "b@2"]})


class DecisionFixture(FigureFixture):
    def setUp(self):
        super().setUp()
        self.shear = Region.objects.get(run=self.venturi.run, page=2)


class HiddenRegionTests(DecisionFixture):
    """A `hide` region stays in the run and disappears from every reader surface."""

    def hide(self, band=Region.Band.HIDE):
        Region.objects.filter(pk=self.venturi.pk).update(band=band, decision="stamp", decision_p=0.97)
        with mock.patch("apps.core.search.embeddings.enabled", return_value=False):
            regions.index_run(self.venturi.run)

    def test_visible_keeps_unknown_keep_and_review(self):
        run = self.venturi.run
        self.assertEqual(run.regions.visible().count(), 2)
        for band in (Region.Band.KEEP, Region.Band.REVIEW):
            Region.objects.filter(pk=self.venturi.pk).update(band=band)
            self.assertEqual(run.regions.visible().count(), 2)
        Region.objects.filter(pk=self.venturi.pk).update(band=Region.Band.HIDE)
        self.assertEqual(list(run.regions.visible()), [self.shear])
        self.assertEqual(run.regions.count(), 2)

    def test_search_skips_it_and_its_document_is_gone(self):
        self.assertTrue(regions.search_regions(fixtures.request_for(self.reader), QUERY, limit=5))
        self.hide()
        self.venturi.refresh_from_db()
        self.assertIsNone(self.venturi.embedding_text)
        self.assertEqual(regions.search_regions(fixtures.request_for(self.reader), QUERY, limit=5), [])
        response = self.client.get("/api/v1/search", {"q": QUERY})
        self.assertEqual(response.json()["response"]["items"], [])

    def test_entry_figures_and_crop_skip_it(self):
        self.hide()
        response = self.client.get(f"/api/v1/entries/{self.entry.pk}/figures")
        self.assertEqual([item["id"] for item in response.json()["response"]], [str(self.shear.pk)])
        with mock.patch("apps.extraction.discovery.get_storage", return_value=FakeStorage()):
            crop = self.client.get(f"/data/v1/entries/{self.entry.pk}/regions/{self.venturi.pk}.webp")
        self.assertEqual(crop.status_code, 404)

    def test_describe_sweep_takes_keep_or_unknown_only(self):
        Region.objects.filter(pk=self.venturi.pk).update(printed_caption=None)
        self.assertIn(self.venturi.pk, set(describe.candidates().values_list("pk", flat=True)))
        for band in (Region.Band.REVIEW, Region.Band.HIDE):
            Region.objects.filter(pk=self.venturi.pk).update(band=band)
            self.assertNotIn(self.venturi.pk, set(describe.candidates().values_list("pk", flat=True)))
        Region.objects.filter(pk=self.venturi.pk).update(band=Region.Band.KEEP)
        self.assertIn(self.venturi.pk, set(describe.candidates().values_list("pk", flat=True)))


class RebandCommandTests(DecisionFixture):
    def call(self, *args, **options):
        out = io.StringIO()
        with mock.patch("apps.core.search.embeddings.enabled", return_value=False):
            call_command("reband_regions", *args, stdout=out, **options)
        return out.getvalue()

    def test_needs_thresholds(self):
        with self.assertRaises(CommandError):
            self.call()

    def test_rebands_from_stored_p_and_reindexes(self):
        Region.objects.filter(pk=self.venturi.pk).update(band="keep", decision="stamp", decision_p=0.95)
        Region.objects.filter(pk=self.shear.pk).update(band="hide", decision="repeated")

        out = self.call("--dry-run", thresholds='{"figure": {"hide": ["stamp"], "upper": 0.9, "lower": 0.6}}')
        self.assertIn("figure keep -> hide: 1", out)
        self.venturi.refresh_from_db()
        self.assertEqual(self.venturi.band, "keep")

        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS):
            self.call()
        self.venturi.refresh_from_db()
        self.shear.refresh_from_db()
        self.assertEqual(self.venturi.band, "hide")
        self.assertIsNone(self.venturi.embedding_text)
        # A rule decision without p is the worker's; it is left alone.
        self.assertEqual(self.shear.band, "hide")

        # A raised threshold shows it again, searchable.
        self.call(thresholds='{"figure": {"hide": ["stamp"], "upper": 0.99, "lower": 0.98}}')
        self.venturi.refresh_from_db()
        self.assertEqual(self.venturi.band, "keep")
        self.assertIsNotNone(self.venturi.embedding_text)

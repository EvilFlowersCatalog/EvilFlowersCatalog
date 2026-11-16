"""#208: the text worker's paragraph kind — stored as sent, banded only under a `paragraph` rule, and hidden
paragraphs kept in the run (the reader) but out of passage search and section texts."""

import io
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant import sections
from apps.assistant.tests import fixtures
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Entry, User
from apps.extraction import discovery, services
from apps.extraction.bands import paragraph_band
from apps.extraction.models import ExtractionRun, Paragraph, Region

RULE = {"paragraph": {"hide": ["references", "toc", "legal", "running_head"], "upper": 0.9, "lower": 0.6}}
BBOX = {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}


def paragraph(seq, text="Some body text.", **extra) -> dict:
    return {"page": 1, "seq": seq, "section": "1 Intro", "bbox": BBOX, "text": text, **extra}


class ParagraphBandTests(SimpleTestCase):
    def test_without_a_rule_nothing_is_banded(self):
        self.assertIsNone(paragraph_band("references", 0.99, config={}))
        self.assertIsNone(paragraph_band("references", 0.99, config={"paragraph": {"hide": ["references"]}}))

    def test_rule(self):
        self.assertEqual(paragraph_band("references", 0.95, RULE), "hide")
        self.assertEqual(paragraph_band("references", 0.7, RULE), "review")
        self.assertEqual(paragraph_band("references", 0.1, RULE), "keep")
        self.assertEqual(paragraph_band("body", 0.99, RULE), "keep")
        self.assertIsNone(paragraph_band(None, 0.99, RULE))
        self.assertIsNone(paragraph_band("references", None, RULE))


class ParagraphIngestTests(TestCase):
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
        response = self.post("/start", {"page_count": 1, "page_sizes": [[595, 842]], "jobs": ["paragraphs"]})
        self.assertEqual(response.status_code, 200, response.content[:400])

    def post(self, path, body):
        return self.client.post(f"{self.base}{path}", body, content_type="application/json", headers=self.headers)

    def paragraphs(self, *items):
        response = self.post("/paragraphs", {"paragraphs": list(items)})
        self.assertEqual(response.status_code, 201, response.content[:400])
        return {row.seq: row for row in self.run.paragraphs.all()}

    def test_stored_as_sent_and_zero_is_a_probability(self):
        rows = self.paragraphs(
            paragraph(0, decision="references", p=0.97, decider_model="laya@1"),
            paragraph(1, decision="body", p=0.0, decider_model="laya@1"),
            paragraph(2),
        )
        self.assertEqual((rows[0].decision, rows[0].decision_p, rows[0].decider_model), ("references", 0.97, "laya@1"))
        self.assertEqual(rows[1].decision_p, 0.0)
        self.assertEqual((rows[2].decision, rows[2].decision_p, rows[2].band), (None, None, None))
        # No rule yet (knowledge#65): nothing is banded, nothing hidden.
        self.assertEqual({row.band for row in rows.values()}, {None})
        self.run.refresh_from_db()
        self.assertEqual(self.run.provenance[services.DECIDER_MODELS], {"paragraphs": ["laya@1"]})

    def test_banded_under_the_rule(self):
        with override_settings(EVILFLOWERS_DECIDER_BANDS=RULE):
            rows = self.paragraphs(paragraph(0, decision="toc", p=0.95), paragraph(1, decision="body", p=0.95))
        self.assertEqual((rows[0].band, rows[1].band), ("hide", "keep"))

    def test_bad_values_are_refused(self):
        for bad in ({"p": 1.5}, {"decision": "x" * 51}):
            response = self.post("/paragraphs", {"paragraphs": [paragraph(0, **bad)]})
            self.assertEqual(response.status_code, 422, bad)
        self.assertFalse(self.run.paragraphs.exists())


class HiddenParagraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        catalog = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.rows, _venturi, _shear = fixtures.publication(cls.reader, catalog)

    def setUp(self):
        patcher = mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ids(self, query):
        found = discovery.search_passages(fixtures.request_for(self.reader), query, per_entry=3)
        return {str(passage.paragraph_id) for item in found.items for passage in item.passages}

    def test_hidden_paragraphs_leave_search_and_section_text_but_stay_in_the_run(self):
        target = self.rows[2]
        word = target.text.split()[1]
        self.assertIn(str(target.pk), self._ids(word))
        Paragraph.objects.filter(pk=target.pk).update(decision="running_head", decision_p=0.99, band=Region.Band.HIDE)
        self.assertNotIn(str(target.pk), self._ids(word))
        self.assertNotIn(" ".join(target.text.split()), sections.section_text(self.extraction, target.section))
        self.assertTrue(self.extraction.paragraphs.filter(pk=target.pk).exists())
        # Review and undecided stay searchable.
        Paragraph.objects.filter(pk=target.pk).update(band=Region.Band.REVIEW)
        self.assertIn(str(target.pk), self._ids(word))

    def test_reband_command(self):
        Paragraph.objects.filter(pk=self.rows[0].pk).update(decision="toc", decision_p=0.95)
        with self.assertRaises(CommandError):
            call_command("reband_paragraphs", stdout=io.StringIO())
        out = io.StringIO()
        call_command(
            "reband_paragraphs",
            "--dry-run",
            "--thresholds",
            '{"paragraph": {"hide": ["toc"], "upper": 0.9}}',
            stdout=out,
        )
        self.assertIn("1 paragraphs change band", out.getvalue())
        self.assertIsNone(Paragraph.objects.get(pk=self.rows[0].pk).band)
        with override_settings(EVILFLOWERS_DECIDER_BANDS={"paragraph": {"hide": ["toc"], "upper": 0.9}}):
            call_command("reband_paragraphs", stdout=io.StringIO())
        self.assertEqual(Paragraph.objects.get(pk=self.rows[0].pk).band, "hide")

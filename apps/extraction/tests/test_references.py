"""IP-030 §3-4: cross-references in paragraph text, and figures as search documents."""

from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.assistant.tests import fixtures
from apps.extraction import references, regions
from apps.extraction.models import Reference, Region


class FindTests(SimpleTestCase):
    def test_slovak_english_and_equation_forms(self):
        found = references.find(
            "Ako ukazuje Obr. 2.1 a obrázku 3, tabuľke 4 aj Table 5-2; podľa rovnice (7) a Section 2.3."
        )
        self.assertEqual(
            [(item["kind"], item["number"]) for item in found],
            [
                ("figure", "2.1"),
                ("figure", "3"),
                ("table", "4"),
                ("table", "5.2"),
                ("equation", "7"),
                ("section", "2.3"),
            ],
        )

    def test_numbers_are_normalised(self):
        self.assertEqual(references.normalise(" 2 . 1. "), "2.1")
        self.assertEqual(references.normalise("3–4"), "3.4")

    def test_caption_number(self):
        region = Region(kind=Region.Kind.FIGURE, printed_caption="Obr. 2.1: Venturiho trubica")
        self.assertEqual(references.caption_number(region), "2.1")
        self.assertIsNone(references.caption_number(Region(kind=Region.Kind.FIGURE, printed_caption="A tube")))


class BuildTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        catalog = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, cls.paragraphs, cls.venturi, cls.shear = fixtures.publication(cls.reader, catalog)

    def test_references_resolve_to_figures_and_sections_and_are_counted(self):
        counts = references.build(self.extraction)
        self.assertEqual(counts["found"], 3)
        self.assertEqual(counts["resolved"], 3)
        rows = {(ref.kind, ref.number): ref for ref in Reference.objects.filter(run=self.extraction)}
        self.assertEqual(rows[("figure", "2.1")].region, self.venturi)
        self.assertEqual(rows[("figure", "1.1")].region, self.shear)
        self.assertEqual(rows[("section", "1")].target_paragraph, self.paragraphs[0])
        self.extraction.refresh_from_db()
        self.assertEqual(self.extraction.provenance["references"]["resolved"], 3)

    def test_rebuilding_replaces_instead_of_adding(self):
        references.build(self.extraction)
        references.build(self.extraction)
        self.assertEqual(Reference.objects.filter(run=self.extraction).count(), 3)

    def test_figure_documents_include_the_referring_paragraph(self):
        references.build(self.extraction)
        with mock.patch("apps.core.search.embeddings.enabled", return_value=False):
            counts = regions.index_run(self.extraction)
        self.assertEqual(counts["documents"], 2)
        self.venturi.refresh_from_db()
        self.assertIn("Venturiho trubica", self.venturi.embedding_text)
        self.assertIn("The Venturi tube in Obr. 2.1", self.venturi.embedding_text)

    def test_a_bare_caption_without_context_is_not_a_document(self):
        self.assertIsNone(regions.document_text(Region(kind=Region.Kind.FIGURE, printed_caption="Figure 3"), None))
        self.assertIsNone(regions.document_text(Region(kind=Region.Kind.EQUATION, latex="E=mc^2"), "context"))

    def test_search_regions_by_words_only_readable_publications(self):
        licensed, run, _rows, _figure, _shear = fixtures.publication(
            self.reader, self.entry.catalog, title="Licensed", readium=True
        )
        for extraction in (self.extraction, run):
            references.build(extraction)
            with mock.patch("apps.core.search.embeddings.enabled", return_value=False):
                regions.index_run(extraction)
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None):
            hits = regions.search_regions(fixtures.request_for(self.reader), "Venturiho trubica")
        self.assertEqual([hit.region_id for hit in hits], [str(self.venturi.pk)])
        self.assertEqual(hits[0].entry_id, str(self.entry.pk))

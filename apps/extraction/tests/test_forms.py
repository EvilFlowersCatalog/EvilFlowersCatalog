"""Ingestion payloads and the reader's page numbering (IP-018)."""

from django.test import SimpleTestCase, override_settings

from apps.assistant.pdf import reader_source_pages
from apps.extraction.forms import ParagraphsForm, RegionsForm, StartRunForm
from apps.extraction.models import EMBEDDING_DIMENSIONS
from apps.extraction.views import _reader_page

BOX = {"x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.6}


class IngestionFormTests(SimpleTestCase):
    def test_region_box_is_clamped_and_optional_fields_are_none(self):
        form = RegionsForm(
            {"regions": [{"kind": "figure", "page": 3, "seq": 0, "bbox": {**BOX, "x1": 1.005}, "caption": ""}]}
        )
        self.assertTrue(form.is_valid(), form.errors)
        region = form.cleaned_data["regions"][0]
        self.assertEqual(region["bbox"]["x1"], 1.0)
        self.assertIsNone(region["caption"])
        self.assertEqual(region["provenance"], {})

    def test_printed_caption_and_description_are_kept_apart(self):
        def region(**fields):
            form = RegionsForm({"regions": [{"kind": "figure", "page": 1, "seq": 0, "bbox": BOX, **fields}]})
            self.assertTrue(form.is_valid(), form.errors)
            return form.cleaned_data["regions"][0]

        # A service that sends both.
        both = region(printed_caption="Figure 2.1: Scaffolding", description="A bar chart.", caption="x")
        self.assertEqual(
            (both["caption"], both["printed_caption"], both["description"]),
            ("Figure 2.1: Scaffolding", "Figure 2.1: Scaffolding", "A bar chart."),
        )
        # image-service before Phase 8 with captions on: the model's text in `caption`.
        legacy = region(
            caption="A bar chart.", provenance={"caption_model": "qwen", "printed_caption": "Figure 2.1: Scaffolding"}
        )
        self.assertEqual(legacy["printed_caption"], "Figure 2.1: Scaffolding")
        self.assertEqual(legacy["description"], "A bar chart.")
        self.assertEqual(legacy["caption"], "Figure 2.1: Scaffolding")
        # Every other service: `caption` is the printed one.
        printed = region(caption="Table 1: Results")
        self.assertEqual((printed["printed_caption"], printed["description"]), ("Table 1: Results", None))

    def test_region_rejects_inverted_box_and_unknown_kind(self):
        self.assertFalse(
            RegionsForm({"regions": [{"kind": "figure", "page": 1, "seq": 0, "bbox": {**BOX, "x1": 0}}]}).is_valid()
        )
        self.assertFalse(RegionsForm({"regions": [{"kind": "chart", "page": 1, "seq": 0, "bbox": BOX}]}).is_valid())

    def test_paragraph_with_sentences_and_embedding(self):
        text = "First sentence. Second one."
        form = ParagraphsForm(
            {
                "paragraphs": [
                    {
                        "page": 1,
                        "seq": 0,
                        "bbox": BOX,
                        "text": text,
                        "embedding": [0.0] * EMBEDDING_DIMENSIONS,
                        "sentences": [
                            {"seq": 0, "start": 0, "end": 15, "page": 1, "quads": [BOX]},
                            {"seq": 1, "start": 16, "end": len(text), "page": 1, "quads": [BOX]},
                        ],
                    }
                ],
                "links": [{"source": 0, "target": 1, "kind": "next"}],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        paragraph = form.cleaned_data["paragraphs"][0]
        self.assertEqual(len(paragraph["sentences"]), 2)
        self.assertEqual(paragraph["text"][16:], "Second one.")

    def test_paragraph_rejects_wrong_dimension_and_offsets_outside_text(self):
        base = {"page": 1, "seq": 0, "bbox": BOX, "text": "Short."}
        self.assertFalse(ParagraphsForm({"paragraphs": [{**base, "embedding": [0.0, 1.0]}]}).is_valid())
        for bad in ("x", [0.0] * (EMBEDDING_DIMENSIONS - 1) + ["0.5"], [0.0] * (EMBEDDING_DIMENSIONS - 1) + [True]):
            self.assertFalse(ParagraphsForm({"paragraphs": [{**base, "embedding": bad}]}).is_valid(), bad)
        nan = [0.0] * (EMBEDDING_DIMENSIONS - 1) + [float("nan")]
        self.assertFalse(ParagraphsForm({"paragraphs": [{**base, "embedding": nan}]}).is_valid())
        ints = ParagraphsForm({"paragraphs": [{**base, "embedding": [0] * EMBEDDING_DIMENSIONS}]})
        self.assertTrue(ints.is_valid(), ints.errors)
        self.assertIsInstance(ints.cleaned_data["paragraphs"][0]["embedding"][0], float)
        self.assertFalse(
            ParagraphsForm(
                {"paragraphs": [{**base, "sentences": [{"seq": 0, "start": 0, "end": 99, "page": 1}]}]}
            ).is_valid()
        )

    def test_start_run(self):
        form = StartRunForm({"page_count": 2, "page_sizes": [[595, 842], [595, 842]], "jobs": ["paragraphs"]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["provenance"], {})
        self.assertFalse(StartRunForm({"page_count": 1, "page_sizes": [595], "jobs": []}).is_valid())


@override_settings(EVILFLOWERS_MODIFIERS={"application/pdf": "apps.core.modifiers.pdf.PDFModifier"})
class ReaderPageTests(SimpleTestCase):
    def test_license_page_is_inserted_as_page_two(self):
        sources = reader_source_pages(4, None, "application/pdf")
        self.assertEqual(sources, [1, None, 2, 3, 4])
        self.assertEqual(_reader_page(sources, 1), 1)
        self.assertEqual(_reader_page(sources, 2), 3)

    def test_range_hides_pages_outside_it(self):
        sources = reader_source_pages(10, "3-5", "application/pdf")
        self.assertEqual(sources, [3, None, 4, 5])
        self.assertIsNone(_reader_page(sources, 7))
        self.assertEqual(_reader_page(sources, 5), 4)

    def test_unmodified_mime_keeps_original_numbering(self):
        self.assertEqual(reader_source_pages(3, None, "application/epub+zip"), [1, 2, 3])

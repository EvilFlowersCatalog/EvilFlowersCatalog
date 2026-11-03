"""Quote resolution (IP-018), ported from monad-knowledge `tests/reading/test_span_highlights.py`."""

import fitz
from django.test import SimpleTestCase

from apps.assistant.highlight import _gate
from apps.assistant.pdf import ReaderDocument
from apps.assistant.quotes import QuoteResolver, quote_in_text

SENTENCE_A = "The receiver counted seated people from the variance of the channel amplitude over time."
SENTENCE_B = "A second experiment repeated the protocol in a corridor with three transmitters placed along one wall."
SENTENCE_C = "Entropy measures the uncertainty of a random variable and is defined over its distribution."


def _pdf(*texts: str) -> fitz.Document:
    pdf = fitz.open()
    for text in texts:
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 400), text, fontsize=11)
    return pdf


def _document(pdf: fitz.Document, source_pages=None) -> ReaderDocument:
    source_pages = source_pages or list(range(1, len(pdf) + 1))
    pages = [pdf[source - 1].get_text("text").strip() if source else "" for source in source_pages]
    return ReaderDocument(pages, toc=[], source_pages=source_pages)


class QuoteResolverTests(SimpleTestCase):
    def setUp(self):
        self.pdf = _pdf(f"{SENTENCE_A} {SENTENCE_B}", SENTENCE_C)
        self.resolver = QuoteResolver(self.pdf, _document(self.pdf))

    def tearDown(self):
        self.pdf.close()

    def test_an_exact_quote_is_placed_on_its_page_as_line_fractions(self):
        located = self.resolver.locate(SENTENCE_A, 1)
        self.assertEqual((located.match, located.page), ("exact", 1))
        self.assertTrue(located.quads)
        for quad in located.quads:
            self.assertTrue(0 <= quad["x0"] < quad["x1"] <= 1)
            self.assertTrue(0 <= quad["y0"] < quad["y1"] <= 1)

    def test_a_wrong_page_guess_still_finds_the_quote(self):
        located = self.resolver.locate(SENTENCE_C, 1)
        self.assertEqual((located.match, located.page), ("exact", 2))

    def test_a_quote_with_one_changed_word_is_a_partial_match(self):
        located = self.resolver.locate(SENTENCE_B.replace("corridor", "hallway"), 1)
        self.assertEqual((located.match, located.page), ("partial", 1))
        self.assertTrue(located.quads)

    def test_a_quote_absent_from_the_pdf_is_missed(self):
        located = self.resolver.locate("Nothing in this sentence appears anywhere in the test document.", 1)
        self.assertEqual(located.match, "missed")

    def test_a_short_quote_is_missed_outright(self):
        self.assertEqual(self.resolver.locate("channel amplitude", 1).match, "missed")

    def test_reader_pages_map_onto_the_source_file(self):
        # Reader page 2 is the license page; reader page 3 is source page 2.
        resolver = QuoteResolver(self.pdf, _document(self.pdf, [1, None, 2]))
        located = resolver.locate(SENTENCE_C, 3)
        self.assertEqual((located.match, located.page), ("exact", 3))


class QuoteGateTests(SimpleTestCase):
    def setUp(self):
        self.document = ReaderDocument([f"{SENTENCE_A}\n{SENTENCE_B}", "An unrelated exam-\nple page."], toc=[])

    def test_quotes_tolerate_whitespace_and_hyphenated_line_breaks(self):
        self.assertTrue(quote_in_text("An unrelated example page.", self.document.page(2)))
        self.assertFalse(quote_in_text("An unrelated sample page.", self.document.page(2)))

    def test_paraphrases_and_unread_pages_are_rejected_and_the_page_corrected(self):
        result = _gate(
            self.document,
            {
                "summary": "s",
                "quotes": [
                    {"page": 2, "text": SENTENCE_B, "note": "wrong page"},
                    {"page": 1, "text": "The receiver counted people sitting down.", "note": "paraphrase"},
                    {"page": 2, "text": "An unrelated example page.", "note": "not read"},
                    {"page": 1, "text": SENTENCE_B, "note": "duplicate"},
                ],
            },
            pages_read=[1],
            tokens=0,
        )
        self.assertEqual([(quote.page, quote.note) for quote in result.quotes], [(1, "wrong page")])
        self.assertEqual(result.rejected, 2)

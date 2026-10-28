import json

from django.test import SimpleTestCase, override_settings

from apps.assistant.explain import READ_PDF_PAGE, SEARCH_PDF, run_document_tool
from apps.assistant.pdf import ReaderDocument


def _read(page: int, count: int = 1) -> dict:
    return {"function": {"name": READ_PDF_PAGE, "arguments": {"page": page, "count": count}}}


@override_settings(EVILFLOWERS_ASSISTANT_MAX_PAGES_READ=3)
class ReadingBudgetTests(SimpleTestCase):
    def setUp(self):
        self.document = ReaderDocument([f"Text of page {n}." for n in range(1, 11)], toc=[])

    def _run(self, call, pages_read):
        return json.loads(run_document_tool(self.document, call, pages_read))

    def test_new_pages_past_the_budget_are_refused(self):
        pages_read = []
        self.assertEqual([p["page"] for p in self._run(_read(1, 3), pages_read)["pages"]], [1, 2, 3])
        refused = self._run(_read(7), pages_read)
        self.assertIn("budget of 3 pages", refused["error"])
        self.assertEqual(pages_read, [1, 2, 3])

    def test_a_page_already_read_is_free_to_read_again(self):
        pages_read = [1, 2, 3]
        self.assertEqual(self._run(_read(2), pages_read)["pages"][0]["page"], 2)

    def test_a_read_crossing_the_budget_returns_the_pages_that_fit(self):
        pages_read = [1, 2]
        result = self._run(_read(5, 3), pages_read)
        self.assertEqual([p["page"] for p in result["pages"]], [5])
        self.assertEqual(pages_read, [1, 2, 5])

    def test_searching_does_not_count(self):
        pages_read = [1, 2, 3]
        self._run({"function": {"name": SEARCH_PDF, "arguments": {"query": "page"}}}, pages_read)
        self.assertEqual(pages_read, [1, 2, 3])

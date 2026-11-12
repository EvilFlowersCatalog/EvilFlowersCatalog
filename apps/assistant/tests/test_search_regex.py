import json

from django.test import SimpleTestCase

from apps.assistant.explain import SEARCH_PDF, run_document_tool
from apps.assistant.pdf import ReaderDocument


def _search(query: str, regex: bool = True) -> dict:
    return {"function": {"name": SEARCH_PDF, "arguments": {"query": query, "regex": regex}}}


class RegexSearchTests(SimpleTestCase):
    def setUp(self):
        self.document = ReaderDocument(
            [
                "Introduction to scaffolding.",
                "Table 2.1 Overview of one-to-one,\ncomputer-based and peer scaffolding. See Table 2.3.",
                "Nothing here.",
                "TABLE 4.2 compares the results.",
            ],
            toc=[],
        )

    def _run(self, call):
        return json.loads(run_document_tool(self.document, call, []))

    def test_matches_are_case_insensitive_and_in_reading_order(self):
        results = self._run(_search(r"table\s+\d\.\d"))["results"]
        self.assertEqual([(hit["page"], hit["matches"]) for hit in results], [(2, 2), (4, 1)])
        self.assertIn("Table 2.1 Overview of one-to-one, computer-based", results[0]["snippet"])

    def test_invalid_pattern_is_an_error_not_a_crash(self):
        self.assertIn("Invalid regular expression", self._run(_search("table (2"))["error"])

    def test_nested_quantifiers_are_refused(self):
        for pattern in ["(a+)+", r"(\w*)*b", "(x+){2,}"]:
            self.assertIn("Nested quantifiers", self._run(_search(pattern))["error"], pattern)

    def test_plain_groups_and_alternation_are_allowed(self):
        results = self._run(_search("scaffold(ing|ed)"))["results"]
        self.assertEqual([hit["page"] for hit in results], [1, 2])

    def test_overlong_pattern_is_refused(self):
        self.assertIn("1-200 characters", self._run(_search("a" * 201))["error"])

    def test_without_regex_the_word_search_runs(self):
        results = self._run(_search("table 2.1", regex=False))["results"]
        self.assertEqual(results[0]["page"], 2)

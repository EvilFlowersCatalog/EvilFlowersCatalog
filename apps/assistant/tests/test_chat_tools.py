"""Chat and explain tools over the reader's copy (IP-018 Phase 8)."""

import json

from django.test import SimpleTestCase

from apps.assistant import explain, turn


class FakeDocument:
    acquisition = None
    page_count = 3

    def search(self, query):
        return [{"page": 2, "snippet": f"... {query} ..."}]


class DocumentToolTests(SimpleTestCase):
    def test_every_offered_document_tool_is_routed(self):
        offered = {spec["function"]["name"] for spec in explain.TOOL_SPECIFICATIONS}
        self.assertEqual(offered, set(explain.DOCUMENT_TOOLS))

    def test_search_paragraphs_falls_back_to_word_search_without_an_index(self):
        call = {"function": {"name": explain.SEARCH_PARAGRAPHS, "arguments": {"query": "entropy"}}}
        result = json.loads(explain.run_document_tool(FakeDocument(), call, []))
        self.assertIn("note", result)
        self.assertEqual(result["results"][0]["page"], 2)

    def test_search_paragraphs_requires_a_query(self):
        call = {"function": {"name": explain.SEARCH_PARAGRAPHS, "arguments": {}}}
        self.assertIn("error", json.loads(explain.run_document_tool(FakeDocument(), call, [])))

    def test_list_regions_without_extraction(self):
        call = {"function": {"name": explain.LIST_REGIONS, "arguments": {}}}
        self.assertIn("error", json.loads(explain.run_document_tool(FakeDocument(), call, [])))

    def test_create_highlights_is_described_for_the_model(self):
        spec = turn.CREATE_HIGHLIGHTS_SPECIFICATION["function"]
        self.assertEqual(spec["name"], turn.CREATE_HIGHLIGHTS)
        self.assertEqual(spec["parameters"]["required"], ["prompt"])

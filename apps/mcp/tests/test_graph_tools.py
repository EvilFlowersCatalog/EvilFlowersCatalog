"""#161: the graph tools answer like the REST graph API, and never past the caller's visibility.

Needs PostgreSQL with Apache AGE (see `apps.graph.tests.test_graph`); skips elsewhere. "Secret" sits in a hidden
catalog and is asked about directly (not found) and indirectly (never a neighbour, section or concept).
"""

from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory

from apps.assistant.models import SectionSummary
from apps.core.tests.test_tracing import recorded
from apps.graph import projection
from apps.graph.tests.test_api import unit
from apps.graph.tests.test_graph import GraphFixture
from apps.mcp import prompts
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry


class GraphToolTests(GraphFixture):
    def setUp(self):
        super().setUp()
        SectionSummary.objects.filter(run=self.secret_run).update(
            concepts=[{"term": "Tlak", "role": "discusses"}, {"term": "Utajený pojem", "role": "defines"}]
        )
        self._project_all()
        self.key = projection.section_key(self.entry.pk, "2 Bernoulli")
        self.other_key = projection.section_key(self.other.pk, "2 Bernoulli")
        self.secret_key = projection.section_key(self.secret.pk, "2 Bernoulli")
        self.server = McpServer(registry)
        cache.clear()

    def call(self, name: str, arguments: dict, user=None) -> dict:
        request = RequestFactory().post("/mcp/v1")
        request.user = user or AnonymousUser()
        response = self.server.handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            LATEST_PROTOCOL_VERSION,
        )
        return response["result"]

    def ok(self, name: str, arguments: dict, user=None) -> dict:
        result = self.call(name, arguments, user)
        self.assertFalse(result.get("isError"), result["content"][0]["text"])
        return result["structuredContent"]

    def error(self, name: str, arguments: dict, user=None) -> str:
        result = self.call(name, arguments, user)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def test_related_sections_by_heading_or_key(self):
        by_heading = self.ok("related_sections", {"entry_id": str(self.entry.pk), "section": "2 Bernoulli"})
        by_key = self.ok("related_sections", {"entry_id": str(self.entry.pk), "section": self.key})
        self.assertEqual(by_heading, by_key)
        self.assertTrue(by_heading["available"])
        [item] = by_heading["items"]
        self.assertEqual(item["entry_id"], str(self.other.pk))
        self.assertEqual(item["title"], "Hydraulics")
        self.assertEqual(item["section"], "2 Bernoulli")
        self.assertEqual(item["first_page"], 3)
        self.assertEqual(item["key"], self.other_key)
        self.assertIn("concepts", item["why"])

    def test_hidden_publication_is_never_reached(self):
        # Asked directly: not found, though it exists and shares a concept.
        self.error("related_sections", {"entry_id": str(self.secret.pk), "section": "2 Bernoulli"})
        # Its key under a visible entry: no way in either.
        self.error("related_sections", {"entry_id": str(self.entry.pk), "section": self.secret_key})
        self.error("section_prerequisites", {"entry_id": str(self.secret.pk), "section": self.secret_key})
        # Asked indirectly: never a neighbour, a defining section or a concept.
        prerequisites = self.ok("section_prerequisites", {"entry_id": str(self.other.pk), "section": "2 Bernoulli"})
        self.assertNotIn(str(self.secret.pk), {item["entry_id"] for item in prerequisites["items"]})
        self.assertEqual({item["key"] for item in self.ok("find_concept", {})["items"]}, {"tlak"})
        self.error("get_concept", {"key": "utajeny pojem"})
        concept = self.ok("get_concept", {"key": "tlak"})["concept"]
        self.assertNotIn(str(self.secret.pk), {section["entry_id"] for section in concept["sections"]})

    def test_a_member_sees_the_hidden_catalog(self):
        self.reader.catalogs.add(self.hidden, through_defaults={"mode": "read"})
        found = self.ok("related_sections", {"entry_id": str(self.secret.pk), "section": "2 Bernoulli"}, self.reader)
        self.assertIn(str(self.entry.pk), {item["entry_id"] for item in found["items"]})

    def test_prerequisites_in_order(self):
        found = self.ok("section_prerequisites", {"entry_id": str(self.other.pk), "section": self.other_key})
        self.assertEqual(
            [(item["entry_id"], item["first_page"], item["later"]) for item in found["items"]],
            [(str(self.entry.pk), 3, False)],
        )

    def test_concepts(self):
        listed = self.ok("find_concept", {"q": "TLAK"})["items"]
        self.assertEqual([(item["key"], item["publications"]) for item in listed], [("tlak", 2)])
        concept = self.ok("get_concept", {"key": "tlak"})["concept"]
        self.assertEqual(concept["term"], "Tlak")
        self.assertEqual(concept["sections"][0]["why"], "defines")
        self.assertEqual(concept["sections"][0]["entry_id"], str(self.entry.pk))

    def test_headings_of_a_licensed_book_are_not_shown(self):
        self.other.config = {"readium_enabled": True}
        self.other.save()
        [item] = self.ok("related_sections", {"entry_id": str(self.entry.pk), "section": "2 Bernoulli"})["items"]
        self.assertIsNone(item["section"])
        self.assertEqual((item["title"], item["first_page"]), ("Hydraulics", 3))

    def test_similar_entries_and_map_stay_within_what_is_visible(self):
        from apps.assistant.tests import fixtures

        # Two subjects of two books each (centring needs more than one pair); Secret is closest to Fluid Mechanics.
        _optics, optics_run, *_ = fixtures.publication(self.reader, self.open, title="Optics")
        _lasers, lasers_run, *_ = fixtures.publication(self.reader, self.open, title="Lasers")
        for run, vector in (
            (self.run, unit(1, 2)),
            (self.other_run, unit(1, 3)),
            (optics_run, unit(5, 6)),
            (lasers_run, unit(5, 7)),
            (self.secret_run, unit(1, 2, 4)),
        ):
            run.embedding = vector
            run.save(update_fields=["embedding"])
        similar = self.ok("similar_entries", {"entry_id": str(self.entry.pk)})["items"]
        self.assertEqual([item["entry_id"] for item in similar], [str(self.other.pk)])
        self.assertEqual(similar[0]["title"], "Hydraulics")
        self.error("similar_entries", {"entry_id": str(self.secret.pk)})
        mapped = self.ok("library_map", {})
        self.assertNotIn(str(self.secret.pk), {node["id"] for node in mapped["nodes"]})
        self.assertEqual(self.ok("library_map", {"catalog_id": str(self.hidden.pk)}), {"nodes": [], "edges": []})

    def test_without_the_graph_results_are_empty_with_a_note(self):
        with mock.patch("apps.graph.age.available", return_value=False):
            related = self.ok("related_sections", {"entry_id": str(self.entry.pk), "section": "2 Bernoulli"})
            concept = self.ok("get_concept", {"key": "tlak"})
            concepts = self.ok("find_concept", {"q": "tlak"})
        self.assertEqual((related["available"], related["items"]), (False, []))
        self.assertTrue(related["note"])
        self.assertEqual((concept["available"], concept["concept"]), (False, None))
        self.assertEqual(concepts["items"], [])

    def test_each_call_records_a_graph_query_span(self):
        with recorded() as exporter:
            self.ok("related_sections", {"entry_id": str(self.entry.pk), "section": "2 Bernoulli"})
        spans = [span for span in exporter.get_finished_spans() if span.name == "graph.query"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].attributes["evilflowers.graph.route"], "mcp.section.related")
        self.assertEqual(spans[0].attributes["evilflowers.graph.rows"], 1)

    def test_study_path_prompt(self):
        text = prompts.get_prompt("study_path", {"topic": "CFL"})["messages"][0]["content"]["text"]
        for tool in ("find_concept", "get_concept", "section_prerequisites", "related_sections"):
            self.assertIn(tool, text)

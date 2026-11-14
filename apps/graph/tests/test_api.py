"""The read-only graph API for the portal (#152): what an anonymous reader may see, and nothing else.

The fixture has three publications; "Secret" sits in a hidden catalog. Every route is asked about it directly
(404) and indirectly (never a neighbour, edge or count).
"""

from unittest import mock

import numpy as np
from django.core.cache import cache

from apps.assistant.models import SectionSummary
from apps.core.tests.test_tracing import recorded, request_span
from apps.extraction.models import EMBEDDING_DIMENSIONS, ParagraphLink
from apps.graph import projection
from apps.graph.tests.test_graph import GraphFixture


def unit(*axes) -> list:
    vector = np.zeros(EMBEDDING_DIMENSIONS)
    for axis in axes:
        vector[axis] = 1.0
    return (vector / np.linalg.norm(vector)).tolist()


class GraphApiTests(GraphFixture):
    def setUp(self):
        super().setUp()
        # A concept only the hidden book uses.
        SectionSummary.objects.filter(run=self.secret_run).update(
            concepts=[{"term": "Tlak", "role": "discusses"}, {"term": "Utajený pojem", "role": "defines"}]
        )
        self._project_all()
        self.key = projection.section_key(self.run.pk, "2 Bernoulli")
        self.secret_key = projection.section_key(self.secret_run.pk, "2 Bernoulli")
        cache.clear()

    def get(self, url, status=200, **headers):
        response = self.client.get(url, **headers)
        self.assertEqual(response.status_code, status, response.content[:400])
        return response

    def data(self, url):
        return self.get(url).json()["response"]

    def test_related_sections(self):
        found = self.data(f"/api/v1/sections/{self.key}/related")
        self.assertEqual([item["entry_id"] for item in found], [str(self.other.pk)])
        self.assertEqual(found[0]["via"], "concepts+similar")
        self.assertEqual(found[0]["first_page"], 3)
        # The hidden book's section: not found, though it exists and shares a concept.
        self.get(f"/api/v1/sections/{self.secret_key}/related", status=404)
        self.get("/api/v1/sections/0000000000/related", status=404)

    def test_prerequisites(self):
        other_key = projection.section_key(self.other_run.pk, "2 Bernoulli")
        found = self.data(f"/api/v1/sections/{other_key}/prerequisites")
        self.assertEqual([(item["entry_id"], item["later"]) for item in found], [(str(self.entry.pk), False)])
        self.get(f"/api/v1/sections/{self.secret_key}/prerequisites", status=404)

    def test_concepts(self):
        listed = self.data("/api/v1/concepts?q=TLAK")
        self.assertEqual([(item["key"], item["publications"]) for item in listed], [("tlak", 2)])
        everything = {item["key"] for item in self.data("/api/v1/concepts")}
        self.assertEqual(everything, {"tlak"})

        concept = self.data("/api/v1/concepts/tlak")
        self.assertEqual(concept["term"], "Tlak")
        self.assertEqual(
            {section["entry_id"] for section in concept["sections"]}, {str(self.entry.pk), str(self.other.pk)}
        )
        self.assertEqual(concept["sections"][0]["role"], "defines")
        self.assertNotIn(str(self.secret.pk), concept["entries"])
        # Used only in the hidden book: as if it did not exist.
        self.get("/api/v1/concepts/utajeny%20pojem", status=404)

    def test_overview_counts_only_what_the_reader_may_see(self):
        overview = self.data("/api/v1/graph/overview")
        self.assertTrue(overview["available"])
        self.assertEqual(overview["publications"], 2)
        self.assertEqual(overview["sections"], 4)
        self.assertEqual(overview["concepts"], 1)
        self.assertEqual(overview["similar"], 1)
        self.assertEqual([item["key"] for item in overview["top_concepts"]], ["tlak"])

    def test_without_the_graph_everything_is_empty(self):
        with mock.patch("apps.graph.age.available", return_value=False):
            self.assertEqual(self.data(f"/api/v1/sections/{self.key}/related"), [])
            self.assertEqual(self.data(f"/api/v1/sections/{self.key}/prerequisites"), [])
            self.assertEqual(self.data("/api/v1/concepts"), [])
            self.get("/api/v1/concepts/tlak", status=404)
            overview = self.data("/api/v1/graph/overview")
        self.assertFalse(overview["available"])
        self.assertEqual(overview["top_concepts"], [])
        self.assertEqual(overview["publications"], 0)

    def _embed_runs(self):
        from apps.assistant.tests import fixtures

        # Two subjects of two books each; Secret is the closest of all to Fluid Mechanics.
        self.optics, optics_run, *_ = fixtures.publication(self.reader, self.open, title="Optics")
        self.lasers, lasers_run, *_ = fixtures.publication(self.reader, self.open, title="Lasers")
        for run, vector in (
            (self.run, unit(1, 2)),
            (self.other_run, unit(1, 3)),
            (optics_run, unit(5, 6)),
            (lasers_run, unit(5, 7)),
            (self.secret_run, unit(1, 2, 4)),
        ):
            run.embedding = vector
            run.save(update_fields=["embedding"])

    def test_library_map(self):
        self._embed_runs()
        response = self.get("/api/v1/entries/map")
        payload = response.json()["response"]
        visible = {str(e.pk) for e in (self.entry, self.other, self.optics, self.lasers)}
        self.assertEqual({node["id"] for node in payload["nodes"]}, visible)
        self.assertEqual(
            {frozenset([edge["source"], edge["target"]]) for edge in payload["edges"]},
            {
                frozenset([str(self.entry.pk), str(self.other.pk)]),
                frozenset([str(self.optics.pk), str(self.lasers.pk)]),
            },
        )
        self.assertTrue(all(edge["score"] > 0 for edge in payload["edges"]))
        self.assertEqual(payload["nodes"][0]["pages"], 6)

        etag = response["ETag"]
        self.get("/api/v1/entries/map", status=304, HTTP_IF_NONE_MATCH=etag)
        # A new category changes the map, so its ETag.
        self._file(self.entry, "53")
        changed = self.get("/api/v1/entries/map", HTTP_IF_NONE_MATCH=etag)
        self.assertNotEqual(changed["ETag"], etag)
        node = next(n for n in changed.json()["response"]["nodes"] if n["id"] == str(self.entry.pk))
        self.assertEqual(node["udc"], ["53"])

    def test_library_map_scoped_by_catalog(self):
        self._embed_runs()
        payload = self.data(f"/api/v1/entries/map?catalog_id={self.hidden.pk}")
        self.assertEqual(payload, {"nodes": [], "edges": []})

    def test_chapters(self):
        intro, bernoulli = self.rows[0:2], self.rows[2:4]
        for source, target in zip(intro, bernoulli):
            ParagraphLink.objects.create(source=source, target=target, kind=ParagraphLink.Kind.SIMILAR, score=0.9)
        response = self.get(f"/api/v1/entries/{self.entry.pk}/chapters")
        payload = response.json()["response"]
        self.assertEqual([section["title"] for section in payload["sections"]], ["1 Introduction", "2 Bernoulli"])
        self.assertEqual(payload["sections"][1]["key"], self.key)
        self.assertEqual(payload["sections"][1]["first_page"], 3)
        self.assertEqual(
            payload["links"],
            [{"source": payload["sections"][0]["key"], "target": self.key, "links": 2}],
        )
        self.get(f"/api/v1/entries/{self.entry.pk}/chapters", status=304, HTTP_IF_NONE_MATCH=response["ETag"])
        self.get(f"/api/v1/entries/{self.secret.pk}/chapters", status=404)

    def test_headings_of_a_licensed_book_are_not_shown(self):
        # A heading is the publication's text: none for a Readium LCP book, as in the assistant's outline.
        self.other.config = {"readium_enabled": True}
        self.other.save()
        related = self.data(f"/api/v1/sections/{self.key}/related")
        self.assertEqual([(item["entry_id"], item["title"]) for item in related], [(str(self.other.pk), None)])
        self.assertEqual(related[0]["first_page"], 3)
        sections = {s["entry_id"]: s["title"] for s in self.data("/api/v1/concepts/tlak")["sections"]}
        self.assertEqual(sections, {str(self.entry.pk): "2 Bernoulli", str(self.other.pk): None})
        chapters = self.data(f"/api/v1/entries/{self.other.pk}/chapters")
        self.assertEqual([section["title"] for section in chapters["sections"]], [None, None])
        self.assertEqual(chapters["sections"][1]["key"], projection.section_key(self.other_run.pk, "2 Bernoulli"))

    def test_each_route_records_a_graph_query_span(self):
        with recorded() as exporter, request_span():
            self.get(f"/api/v1/sections/{self.key}/related")
        spans = [span for span in exporter.get_finished_spans() if span.name == "graph.query"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].attributes["evilflowers.graph.route"], "section.related")
        self.assertEqual(spans[0].attributes["evilflowers.graph.rows"], 1)

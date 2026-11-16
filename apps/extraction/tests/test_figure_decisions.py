"""#205: the decision model's label as a search facet and, once calibrated, in the order; undecided regions as before."""

from django.test import override_settings

from apps.extraction import regions
from apps.extraction.figure_search import split_kinds
from apps.extraction.models import Region
from apps.extraction.tests.test_figure_search import QUERY, FigureFixture

BANDS = {"figure": {"hide": ["stamp"], "upper": 0.9, "lower": 0.6}, "table": {"upper": 0.7, "lower": 0.3}}


class FigureDecisionTests(FigureFixture):
    def search(self, query=QUERY, status=200, **params):
        response = self.client.get("/api/v1/search", {"q": query, **params})
        self.assertEqual(response.status_code, status, response.content[:400])
        return response.json()["response"] if status == 200 else response

    def setUp(self):
        super().setUp()
        run = self.venturi.run
        self.chart = self._region(run, 10, "chart", 0.82, Region.Band.KEEP)
        self.diagram = self._region(run, 11, "diagram", 0.7, Region.Band.REVIEW)

    @staticmethod
    def _region(run, seq, decision, p, band, kind=Region.Kind.FIGURE, score=None):
        return Region.objects.create(
            run=run,
            kind=kind,
            page=5,
            seq=seq,
            bbox={"x0": 0, "y0": 0, "x1": 1, "y1": 1},
            caption=f"{QUERY} {decision}",
            printed_caption=f"{QUERY} {decision}",
            embedding_text=f"{QUERY} {decision}",
            decision=decision,
            decision_p=p,
            decision_score=score,
            band=band,
        )

    def test_kind_facet_filters_by_label_and_returns_it(self):
        found = self.search(kind="chart")
        self.assertEqual([item["region_id"] for item in found["items"]], [str(self.chart.pk)])
        self.assertEqual((found["items"][0]["decision"], found["items"][0]["decision_p"]), ("chart", 0.82))
        both = {item["region_id"] for item in self.search(kind="chart,diagram")["items"]}
        self.assertEqual(both, {str(self.chart.pk), str(self.diagram.pk)})
        self.assertEqual(self.search(kind="map")["items"], [])
        self.search(kind="sculpture", status=400)

    def test_undecided_regions_carry_nulls(self):
        undecided = next(item for item in self.search()["items"] if item["region_id"] == str(self.venturi.pk))
        self.assertIsNone(undecided["decision"])
        self.assertIsNone(undecided["decision_p"])

    def test_review_sorts_after_keep_only_with_thresholds(self):
        request = self._request()
        # Same words, so the fused rank ties among the labelled ones; review last once thresholds exist.
        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS):
            hits = regions.search_regions(request, QUERY, limit=5, labels=["chart", "diagram"])
        self.assertEqual([hit.region_id for hit in hits][-1], str(self.diagram.pk))
        Region.objects.filter(pk=self.chart.pk).update(band=Region.Band.REVIEW)
        Region.objects.filter(pk=self.diagram.pk).update(band=Region.Band.KEEP)
        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS):
            hits = regions.search_regions(request, QUERY, limit=5, labels=["chart", "diagram"])
        self.assertEqual([hit.region_id for hit in hits][-1], str(self.chart.pk))

    def test_prefer_moves_a_label_first_without_dropping_others(self):
        request = self._request()
        hits = regions.search_regions(request, QUERY, limit=5, prefer="diagram")
        self.assertEqual(hits[0].region_id, str(self.diagram.pk))
        self.assertEqual(len(hits), 3)

    def _request(self):
        from apps.assistant.tests import fixtures

        return fixtures.request_for(self.reader)


class WantedKindTests(FigureFixture):
    def test_questions(self):
        self.assertEqual(regions.wanted_kind("Show the graph of pressure along the pipe"), "chart")
        self.assertEqual(regions.wanted_kind("Ukáž mi graf tlaku"), "chart")
        self.assertEqual(regions.wanted_kind("schéma regulačného obvodu"), "diagram")
        self.assertEqual(regions.wanted_kind("Tabuľka hustôt"), "table")
        self.assertIsNone(regions.wanted_kind("What is Bernoulli's principle?"))

    def test_split_kinds(self):
        self.assertEqual(split_kinds(["figure", "table"]), (["figure", "table"], None))
        self.assertEqual(split_kinds(["table"]), (["table"], None))
        self.assertEqual(split_kinds(["chart"]), (["figure", "table"], ["chart"]))
        self.assertEqual(split_kinds(["chart", "table"]), (["figure", "table"], ["chart", "table"]))

"""#206: typed MENTIONS (explains / uses / named) and SHOWS (refers or not); the decider is faked over HTTP."""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant.models import SectionSummary
from apps.assistant.tests import fixtures
from apps.decider import mentions, tasks
from apps.decider.models import Band, MentionRelation, ShowsRelation
from apps.extraction import locators, references
from apps.graph import age, projection
from apps.graph.tests.test_graph import GraphFixture

DECIDER = "http://decider.test"
MODEL = "laya-multilingual@0.4.34"
RULES = {"mention": {"upper": 0.9}, "shows": {"upper": 0.9}}


def reply(answers: dict, model=MODEL):
    """Answers per question key: each state gets the next `(choice, p)` of its question's list."""

    def post(url, json=None, timeout=None):
        assert url == f"{DECIDER}/v1/systemone/batch", url
        [question] = json["questions"]
        results = []
        for _ in json["states"]:
            picked, p = answers[question].pop(0)
            results.append({"answers": {question: {"choice": picked, "probabilities": {picked: p}}}, "model": model})
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": results}
        post.calls.append(json)
        return response

    post.calls = []
    return post


class BandTests(SimpleTestCase):
    def test_named_and_no_hide_only_above_the_threshold(self):
        self.assertEqual(mentions.band("mention", "named", 0.95, RULES), Band.HIDE)
        self.assertEqual(mentions.band("mention", "named", 0.5, RULES), Band.KEEP)
        self.assertEqual(mentions.band("mention", "explains", 0.99, RULES), Band.KEEP)
        self.assertEqual(mentions.band("shows", "no", 0.95, RULES), Band.HIDE)
        self.assertEqual(mentions.band("shows", "no", 0.99, {}), Band.KEEP)


@override_settings(EVILFLOWERS_DECIDER_URL=DECIDER, EVILFLOWERS_DECIDER_BANDS=RULES)
class MentionSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        admin = fixtures.user("admin")
        catalog = fixtures.catalog(admin)
        cls.entry, cls.extraction, *_ = fixtures.publication(admin, catalog)
        references.build(cls.extraction)
        locators.assign(cls.extraction)
        SectionSummary.objects.create(
            run=cls.extraction,
            entry=cls.entry,
            section="2 Bernoulli",
            first_page=3,
            last_page=5,
            summary="s",
            digest="d",
            concepts=[{"term": "Venturi", "role": "defines"}, {"term": "energy", "role": "discusses"}],
        )

    def sweep(self, answers):
        fake = reply(answers)
        with patch("apps.decider.client.requests.post", side_effect=fake):
            return mentions.sweep(), fake.calls

    def test_types_mentions_and_shows_and_asks_once_per_model(self):
        shows = len(mentions.shows_candidates(None, 100))
        self.assertGreater(shows, 0)
        report, calls = self.sweep(
            {"mention": [("explains", 0.8), ("named", 0.95)], "shows": [("yes", 0.9)] + [("no", 0.95)] * shows}
        )
        self.assertEqual(report["mention"]["typed"], 2)
        self.assertEqual(report["mention"]["hidden"], 1)
        self.assertEqual(report["shows"]["typed"], shows)
        # The paragraph that names the concept, not just the section's opening.
        self.assertIn("Concept: Venturi", calls[0]["states"][0]["body"])
        self.assertIn("Venturi tube", calls[0]["states"][0]["body"])
        self.assertIn("Figure caption:", calls[1]["states"][0]["body"])
        explains = MentionRelation.objects.get(relation="explains")
        self.assertEqual((explains.p, explains.decider_model, explains.band), (0.8, MODEL, Band.KEEP))
        self.assertEqual(MentionRelation.objects.get(relation="named").band, Band.HIDE)
        self.assertEqual(ShowsRelation.objects.count(), shows)

        report, calls = self.sweep({"mention": [], "shows": []})
        self.assertEqual((report["mention"]["typed"], report["shows"]["typed"], calls), (0, 0, []))

    def test_a_failure_stores_nothing(self):
        with patch("apps.decider.client.requests.post", side_effect=requests.Timeout("slow")):
            report = mentions.sweep()
        self.assertEqual(report["mention"]["typed"], 0)
        self.assertGreater(report["mention"]["failed"], 0)
        self.assertFalse(MentionRelation.objects.exists())

    @override_settings(EVILFLOWERS_DECIDER_URL="")
    def test_off_without_a_url(self):
        self.assertIn("skipped", tasks.mention_sweep())


class TypedEdgeProjectionTests(GraphFixture):
    """Needs Apache AGE; skips elsewhere."""

    def test_hidden_answers_are_not_drawn_and_kind_is_on_the_edge(self):
        locators.assign(self.run)
        section = projection.section_key(self.entry.pk, "2 Bernoulli")
        concept = projection.concept_key("Tlak")
        MentionRelation.objects.create(
            section_key=section, concept_key=concept, relation="explains", p=0.8, decider_model=MODEL
        )
        self._project_all()
        rows = age.cypher(
            "MATCH (s:Section {key: $key})-[m:MENTIONS]->(c:Concept) RETURN m.kind, m.p",
            {"key": section},
            ("kind", "p"),
        )
        self.assertEqual([(row[0], float(row[1])) for row in rows], [("explains", 0.8)])
        shows_before = projection.counts()["SHOWS"]
        self.assertGreater(shows_before, 0)

        MentionRelation.objects.filter(section_key=section).update(relation="named", p=0.99, band=Band.HIDE)
        from apps.extraction.models import Reference

        for paragraph_key, figure_key in Reference.objects.filter(run=self.run, region__isnull=False).values_list(
            "paragraph__locator_key", "region__locator_key"
        ):
            ShowsRelation.objects.get_or_create(
                paragraph_key=paragraph_key,
                figure_key=figure_key,
                defaults={"answer": "no", "p": 0.99, "band": Band.HIDE, "decider_model": MODEL},
            )
        self._project_all()
        self.assertEqual(age.cypher("MATCH (s:Section {key: $key})-[m:MENTIONS]->() RETURN m", {"key": section}), [])
        self.assertLess(projection.counts()["SHOWS"], shows_before)

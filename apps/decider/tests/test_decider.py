"""#190 typed section pairs, #191 subject suggestions: the decider is faked at the HTTP boundary."""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from apps.assistant.models import SectionSummary
from apps.assistant.tests import fixtures
from apps.core.models import Category
from apps.decider import client, edges, subjects
from apps.decider.models import Band, SectionRelation, SubjectSuggestion
from apps.extraction import similarity
from apps.extraction.models import SectionSimilarity
from apps.graph import projection
from apps.graph.projection import section_key

DECIDER = "http://decider.test"
MODEL = "laya-multilingual@0.4.34"


def reply(choices, model=MODEL, question="relation"):
    """A `/v1/systemone/batch` answer: one result per state, each the next of `choices` as (choice, p)."""

    def post(url, json=None, timeout=None):
        assert url == f"{DECIDER}/v1/systemone/batch", url
        assert json["model"] == "multilingual"
        assert len(json["states"]) <= client.BATCH
        criteria = json["questions"][question]["criteria"]
        results = []
        for _ in json["states"]:
            picked, p = choices.pop(0) if choices else (next(iter(criteria)), 0.5)
            if callable(picked):
                picked = picked(criteria)
            results.append(
                {
                    "answers": {question: {"choice": picked, "probabilities": {picked: p}, "confidence": p}},
                    "model": model,
                }
            )
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": results}
        post.calls.append(json)
        return response

    post.calls = []
    return post


@override_settings(EVILFLOWERS_DECIDER_URL=DECIDER)
class ClientTests(SimpleTestCase):
    def test_answers_and_the_model_that_gave_them(self):
        with patch("apps.decider.client.requests.post", side_effect=reply([("pre", 0.8)])):
            [answer], model = client.ask([{"body": "x"}], edges.QUESTION)
        self.assertEqual(model, MODEL)
        self.assertEqual(answer["relation"].choice, "pre")
        self.assertEqual(answer["relation"].p, 0.8)

    def test_failures_raise(self):
        with patch("apps.decider.client.requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(client.DeciderError):
                client.ask([{"body": "x"}], edges.QUESTION)
        with patch("apps.decider.client.requests.post", return_value=MagicMock(status_code=422, text="image")):
            with self.assertRaises(client.DeciderError):
                client.ask([{"body": "x"}], edges.QUESTION)
        with self.assertRaises(client.DeciderError):
            client.ask([{"body": "x"}] * (client.BATCH + 1), edges.QUESTION)

    @override_settings(EVILFLOWERS_DECIDER_URL="")
    def test_off_without_a_url(self):
        self.assertFalse(client.enabled())
        with self.assertRaises(client.DeciderError):
            client.ask([{"body": "x"}], edges.QUESTION)


class BandTests(SimpleTestCase):
    RULE = {"edge": {"hide": ["none"], "upper": 0.9, "lower": 0.7}}

    def test_unrelated_with_high_p_is_hidden(self):
        self.assertEqual(edges.band("none", 0.95, self.RULE), Band.HIDE)
        self.assertEqual(edges.band("none", 0.8, self.RULE), Band.REVIEW)
        self.assertEqual(edges.band("none", 0.5, self.RULE), Band.KEEP)
        self.assertEqual(edges.band("pre", 0.99, self.RULE), Band.KEEP)

    def test_nothing_hidden_without_thresholds(self):
        self.assertEqual(edges.band("none", 0.99, {}), Band.KEEP)


@override_settings(EVILFLOWERS_DECIDER_URL=DECIDER, EVILFLOWERS_DECIDER_BANDS={"edge": {"upper": 0.9}})
class EdgeSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        cls.fluids, cls.fluids_run, *_ = fixtures.publication(cls.admin, cls.catalog)
        cls.pipes, cls.pipes_run, *_ = fixtures.publication(cls.admin, cls.catalog, title="Pipe Flow")
        SectionSimilarity.objects.create(
            source=cls.fluids_run,
            source_section="2 Bernoulli",
            target=cls.pipes_run,
            target_section="2 Bernoulli",
            score=0.9,
        )
        SectionSimilarity.objects.create(
            source=cls.fluids_run,
            source_section="1 Introduction",
            target=cls.pipes_run,
            target_section="1 Introduction",
            score=0.7,
        )
        SectionSummary.objects.create(
            run=cls.fluids_run,
            entry=cls.fluids,
            section="2 Bernoulli",
            first_page=3,
            last_page=5,
            summary="Pressure falls where speed rises.",
            digest="x",
        )
        cls.bernoulli = (section_key(cls.fluids.pk, "2 Bernoulli"), section_key(cls.pipes.pk, "2 Bernoulli"))
        cls.intro = (section_key(cls.fluids.pk, "1 Introduction"), section_key(cls.pipes.pk, "1 Introduction"))

    def sweep(self, choices, model=MODEL):
        fake = reply(choices, model)
        with patch("apps.decider.client.requests.post", side_effect=fake):
            return edges.sweep(), fake.calls

    def test_types_every_pair_most_similar_first_and_hides_unrelated(self):
        report, calls = self.sweep([("pre", 0.8), ("none", 0.95)])
        self.assertEqual(report["typed"], 2)
        self.assertEqual(report["hidden"], 1)
        self.assertEqual(report["relations"], {"pre": 1, "none": 1})
        [call] = calls
        # The summary when there is one, else the first paragraphs.
        self.assertIn("Pressure falls where speed rises.", call["states"][0]["body"])
        self.assertIn("Fluids flow.", call["states"][1]["body"])
        relation = SectionRelation.objects.get(a_key=self.bernoulli[0], b_key=self.bernoulli[1])
        self.assertEqual((relation.relation, relation.p, relation.decider_model), ("pre", 0.8, MODEL))
        self.assertEqual(SectionRelation.objects.get(a_key=self.intro[0]).band, Band.HIDE)

    def test_a_pair_is_asked_once_per_model_and_survives_a_rebuild(self):
        self.sweep([("pre", 0.8), ("loose", 0.6)])
        # The weekly rebuild writes the pairs again, the other way round.
        SectionSimilarity.objects.all().delete()
        SectionSimilarity.objects.create(
            source=self.pipes_run,
            source_section="2 Bernoulli",
            target=self.fluids_run,
            target_section="2 Bernoulli",
            score=0.9,
        )
        report, calls = self.sweep([])
        self.assertEqual((report["typed"], calls), (0, []))
        self.assertEqual(SectionRelation.objects.count(), 2)
        # A new model asks again, in the orientation asked before.
        with patch("apps.decider.edges.latest_model", return_value="laya@0.5"):
            report, calls = self.sweep([("same", 0.7)], model="laya@0.5")
        self.assertEqual(report["typed"], 1)
        self.assertTrue(calls[0]["states"][0]["body"].startswith("Section A: 2 Bernoulli\nPressure falls"))
        relation = SectionRelation.objects.get(a_key=self.bernoulli[0])
        self.assertEqual((relation.relation, relation.decider_model), ("same", "laya@0.5"))

    def test_a_decider_failure_stops_the_night_and_stores_nothing(self):
        with patch("apps.decider.client.requests.post", side_effect=requests.Timeout("slow")):
            report = edges.sweep()
        self.assertEqual((report["typed"], report["failed"]), (0, 2))
        self.assertFalse(SectionRelation.objects.exists())

    def test_readers_see_the_relation_from_their_side_and_never_a_hidden_pair(self):
        self.sweep([("pre", 0.8), ("none", 0.95)])
        relation = SectionRelation.objects.get(a_key=self.bernoulli[0])
        self.assertEqual(edges.perspective(relation, self.bernoulli[0]), "next")
        self.assertEqual(edges.perspective(relation, self.bernoulli[1]), "prerequisite")
        drawn = projection._similar_sections(self.fluids_run)
        self.assertEqual([(item["a"], item["relation"]) for item in drawn], [(self.bernoulli[0], "pre")])

    def test_related_sections_carry_the_relation(self):
        from apps.graph import queries

        self.sweep([("example", 0.7), ("none", 0.95)])

        def item(key):
            return {
                "key": key,
                "entry_id": str(self.pipes.pk),
                "run_id": "",
                "title": "",
                "first_page": 1,
                "score": 2.7,
                "via": "similar",
            }

        untyped, typed = queries._with_relations(self.bernoulli[0], [item(self.intro[1]), item(self.bernoulli[1])])
        self.assertEqual((untyped["relation"], untyped["relation_p"]), (None, None))
        self.assertEqual((typed["relation"], typed["relation_p"]), ("example", 0.7))
        # From the other book's side, its Bernoulli section is the general idea of this one's example.
        [back] = queries._with_relations(self.bernoulli[1], [item(self.bernoulli[0])])
        self.assertEqual(back["relation"], "general")
        # The hidden pair is left out.
        self.assertEqual(queries._with_relations(self.intro[0], [item(self.intro[1])]), [])

    def test_compute_run_keeps_the_relations(self):
        self.sweep([("pre", 0.8), ("loose", 0.6)])
        with (
            patch.object(similarity, "section_vectors", return_value=([], None)),
            patch.object(similarity, "_pairs", return_value=iter(())),
        ):
            similarity.compute_run(self.fluids_run)
        self.assertEqual(SectionRelation.objects.count(), 2)


@override_settings(
    EVILFLOWERS_DECIDER_URL=DECIDER,
    EVILFLOWERS_DECIDER_BANDS={"subject": {"upper": 0.8, "lower": 0.4}},
    EVILFLOWERS_DECIDER_SUBJECT_SCHEMES=["MDT"],
)
class SubjectSweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = fixtures.user("admin")
        cls.catalog = fixtures.catalog(cls.admin)
        terms = {
            "5": "Natural sciences",
            "53": "Physics",
            "532": "Fluid mechanics",
            "62": "Engineering",
            "621.3": "Electrical engineering",
            "531/534": "Mechanics",
        }
        cls.categories = {
            term: Category.objects.create(
                creator=cls.admin, catalog=cls.catalog, term=term, scheme="MDT", label={"en": label}
            )
            for term, label in terms.items()
        }
        cls.tag = Category.objects.create(creator=cls.admin, catalog=cls.catalog, term="Python", scheme="calibre")
        cls.fluids, *_ = fixtures.publication(cls.admin, cls.catalog)
        cls.fluids.summary = "Fluids in pipes and channels."
        cls.fluids.save()
        cls.pipes, *_ = fixtures.publication(cls.admin, cls.catalog, title="Pipe Flow")
        cls.filed = fixtures.publication(cls.admin, cls.catalog, title="Filed Twice")[0]
        cls.filed.categories.add(cls.categories["53"], cls.categories["62"])

    def pick(self, term):
        category = self.categories[term]
        return lambda criteria: str(category.pk)

    def sweep(self, choices):
        fake = reply(choices, question="category")
        with patch("apps.decider.client.requests.post", side_effect=fake):
            return subjects.sweep(), fake.calls

    def test_tree_from_the_notation(self):
        top, children = subjects.tree(self.categories.values())
        self.assertEqual([category.term for category in top], ["5", "62"])
        self.assertEqual(sorted(c.term for c in children[self.categories["5"].pk]), ["53"])
        self.assertEqual(sorted(c.term for c in children[self.categories["53"].pk]), ["531/534", "532"])
        self.assertEqual([c.term for c in children[self.categories["62"].pk]], ["621.3"])

    def test_two_levels_then_filed_queued_or_skipped(self):
        # fluids: 5 (0.9) then 53 (0.95) -> 0.855 filed; pipes: 62 (0.9) then 621.3 (0.5) -> 0.45 queued.
        report, calls = self.sweep(
            [(self.pick("5"), 0.9), (self.pick("62"), 0.9), (self.pick("53"), 0.95), (self.pick("621.3"), 0.5)]
        )
        self.assertEqual((report["asked"], report["filed"], report["queued"]), (2, 1, 1))
        first = calls[0]
        self.assertEqual(
            set(first["questions"]["category"]["criteria"].values()), {"5 Natural sciences", "62 Engineering"}
        )
        self.assertEqual(first["states"][0]["title"], "Fluid Mechanics")
        self.assertEqual(first["states"][0]["abstract"], "Fluids in pipes and channels.")
        self.assertEqual(first["states"][0]["toc"], "1 Introduction; 2 Bernoulli")
        self.assertIn("5 Natural sciences (general)", calls[1]["questions"]["category"]["criteria"].values())
        self.assertEqual(list(self.fluids.categories.all()), [self.categories["53"]])
        suggestion = SubjectSuggestion.objects.get(entry=self.pipes)
        self.assertEqual(
            (suggestion.status, suggestion.category, round(suggestion.p, 2)),
            (SubjectSuggestion.Status.PENDING, self.categories["621.3"], 0.45),
        )
        self.assertFalse(self.pipes.categories.exists())
        # The entry with two subjects is not asked; nobody is asked twice by one model.
        self.assertFalse(SubjectSuggestion.objects.filter(entry=self.filed).exists())
        report, calls = self.sweep([])
        self.assertEqual((report["asked"], calls), (0, []))

    def test_a_librarian_accepts_or_rejects_and_nothing_is_replaced(self):
        self.fluids.categories.add(self.tag)
        self.sweep([(self.pick("62"), 0.6), (self.pick("62"), 0.6), (self.pick("621.3"), 0.9), (self.pick("62"), 0.9)])
        fluids = SubjectSuggestion.objects.get(entry=self.fluids)
        pipes = SubjectSuggestion.objects.get(entry=self.pipes)
        self.assertTrue(subjects.accept(fluids, self.admin))
        self.assertTrue(subjects.reject(pipes, self.admin))
        self.assertFalse(subjects.accept(pipes, self.admin))
        self.assertEqual(set(self.fluids.categories.all()), {self.tag, self.categories["621.3"]})
        self.assertFalse(self.pipes.categories.exists())
        fluids.refresh_from_db()
        self.assertEqual((fluids.status, fluids.decided_by), (SubjectSuggestion.Status.ACCEPTED, self.admin))

    @override_settings(EVILFLOWERS_DECIDER_BANDS={})
    def test_without_thresholds_everything_waits_for_review(self):
        report, _ = self.sweep([(self.pick("62"), 0.99), (self.pick("62"), 0.99), (self.pick("62"), 0.99)] * 2)
        self.assertEqual((report["filed"], report["queued"]), (0, 2))


class KindsTests(SimpleTestCase):
    def test_sweeps_are_maintenance_kinds_on_the_maintenance_queue(self):
        from django.conf import settings

        from apps.tasks.application.registry import registry

        for kind, task in (
            ("maintenance.decider_edge_sweep", "apps.decider.tasks.edge_sweep"),
            ("maintenance.decider_subject_sweep", "apps.decider.tasks.subject_sweep"),
        ):
            self.assertEqual(registry.get(kind).task, task)
            self.assertIn(task, settings.EVILFLOWERS_MAINTENANCE_TASKS)

    @override_settings(EVILFLOWERS_DECIDER_URL="")
    def test_off_without_a_url(self):
        from apps.decider import tasks

        self.assertIn("skipped", tasks.edge_sweep())
        self.assertIn("skipped", tasks.subject_sweep())

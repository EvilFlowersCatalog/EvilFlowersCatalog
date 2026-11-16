"""#203: related sections fill the gap hidden pairs leave, the relation weights them above the threshold only, and
the typed edges answer "read first" and "examples"."""

from django.test import TestCase, override_settings

from apps.decider.edges import relation_factor
from apps.decider.models import Band, SectionRelation
from apps.graph import projection, queries
from apps.graph.tests.test_graph import GraphFixture

BANDS = {"edge": {"hide": ["none"], "upper": 0.9}}
WEIGHTS = {"same": 1.2, "pre": 1.1, "example": 1.0, "loose": 0.7}


def _item(key: str, score: float) -> dict:
    return {"key": key, "entry_id": "e", "run_id": "r", "title": key, "first_page": 1, "score": score}


class RankRelatedTests(TestCase):
    def test_hidden_pairs_do_not_shorten_the_list(self):
        similar = [_item(f"k{index}", 0.9 - index * 0.01) for index in range(6)]
        for key in ("k0", "k1"):
            SectionRelation.objects.create(
                a_key="s", b_key=key, relation="none", p=0.99, band=Band.HIDE, decider_model="m"
            )
        found = queries.rank_related("s", [], similar, 3)
        self.assertEqual([item["key"] for item in found], ["k2", "k3", "k4"])
        self.assertNotIn("_relation", found[0])
        self.assertNotIn("similar", found[0])

    def test_without_thresholds_the_relation_changes_nothing(self):
        SectionRelation.objects.create(a_key="s", b_key="k1", relation="loose", p=0.99, decider_model="m")
        with override_settings(EVILFLOWERS_DECIDER_BANDS={}, EVILFLOWERS_DECIDER_RELATION_WEIGHTS=WEIGHTS):
            found = queries.rank_related("s", [], [_item("k1", 0.9), _item("k2", 0.85)], 2)
        self.assertEqual([item["key"] for item in found], ["k1", "k2"])
        self.assertEqual(found[0]["score"], 2.7)
        self.assertEqual(found[0]["relation"], "loose")

    def test_factor_applies_above_the_threshold_only(self):
        SectionRelation.objects.create(a_key="s", b_key="k1", relation="loose", p=0.95, decider_model="m")
        SectionRelation.objects.create(a_key="s", b_key="k3", relation="loose", p=0.5, decider_model="m")
        SectionRelation.objects.create(a_key="k2", b_key="s", relation="same", p=0.95, decider_model="m")
        similar = [_item("k1", 0.9), _item("k2", 0.85), _item("k3", 0.8)]
        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS, EVILFLOWERS_DECIDER_RELATION_WEIGHTS=WEIGHTS):
            found = queries.rank_related("s", [], similar, 3)
        scores = {item["key"]: item["score"] for item in found}
        self.assertEqual([item["key"] for item in found], ["k2", "k3", "k1"])
        self.assertEqual(scores["k2"], round(0.85 * 3 * 1.2, 4))
        self.assertEqual(scores["k3"], round(0.8 * 3, 4))  # p below `upper`: no factor
        self.assertEqual(scores["k1"], round(0.9 * 3 * 0.7, 4))

    def test_concepts_are_not_weighted(self):
        SectionRelation.objects.create(a_key="s", b_key="k1", relation="loose", p=0.95, decider_model="m")
        with override_settings(EVILFLOWERS_DECIDER_BANDS=BANDS, EVILFLOWERS_DECIDER_RELATION_WEIGHTS=WEIGHTS):
            found = queries.rank_related("s", [_item("k1", 2)], [_item("k1", 0.9)], 1)
        self.assertEqual(found[0]["via"], "concepts+similar")
        self.assertEqual(found[0]["score"], round(2 * 3 + 0.9 * 3 * 0.7, 4))

    def test_relation_factor_defaults(self):
        relation = SectionRelation(a_key="a", b_key="b", relation="same", p=0.95)
        self.assertEqual(relation_factor(None, BANDS, WEIGHTS), 1.0)
        self.assertEqual(relation_factor(relation, {}, WEIGHTS), 1.0)
        self.assertEqual(relation_factor(relation, BANDS, {}), 1.0)
        self.assertEqual(relation_factor(relation, BANDS, WEIGHTS), 1.2)
        relation.p = None
        self.assertEqual(relation_factor(relation, BANDS, WEIGHTS), 1.0)


class TypedEdgeQueryTests(GraphFixture):
    """Needs Apache AGE; skips elsewhere (see `test_graph`)."""

    def _typed(self, relation: str, p: float = 0.8):
        a = projection.section_key(self.entry.pk, "2 Bernoulli")
        b = projection.section_key(self.other.pk, "2 Bernoulli")
        SectionRelation.objects.create(a_key=a, b_key=b, relation=relation, p=p, decider_model="m")
        self._project_all()
        return a, b

    def test_reading_path_follows_prerequisite_of(self):
        a, b = self._typed("pre")  # A (entry) is a prerequisite of B (other)
        entries = [str(self.entry.pk), str(self.other.pk)]
        path = queries.reading_path(b, entries)
        self.assertEqual([(item["key"], item["depth"]) for item in path], [(a, 1)])
        self.assertEqual(queries.reading_path(a, entries), [])
        # Typed prerequisites come first in `prerequisites`, labelled.
        found = queries.prerequisites(b, entries)
        self.assertEqual(found[0]["key"], a)
        self.assertEqual(found[0]["via"], "relation")
        self.assertEqual(len({item["key"] for item in found}), len(found))
        # Not the reader's publication: nothing.
        self.assertEqual(queries.reading_path(b, [str(self.other.pk)]), [])

    def test_examples_follow_example_of(self):
        a, b = self._typed("example")  # B is an example of A
        entries = [str(self.entry.pk), str(self.other.pk)]
        found = queries.examples(a, entries)
        self.assertEqual([item["key"] for item in found], [b])
        self.assertAlmostEqual(found[0]["score"], 0.8)
        self.assertEqual(queries.examples(b, entries), [])

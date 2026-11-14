"""Cross-book similarity (#151): section pairs from centred centroids, mutual nearest publications."""

import numpy as np
from django.test import SimpleTestCase, TestCase

from apps.assistant.tests import fixtures
from apps.extraction import similarity
from apps.extraction.models import EMBEDDING_DIMENSIONS, SectionSimilarity

#: A component every vector shares, as e5 vectors do: raw cosine is high for any pair.
COMMON = 3.0


def vector(*topics: int, seed: int = 0) -> np.ndarray:
    noise = np.random.default_rng(seed).normal(0, 0.005, EMBEDDING_DIMENSIONS)
    value = np.full(EMBEDDING_DIMENSIONS, COMMON / np.sqrt(EMBEDDING_DIMENSIONS)) + noise
    for topic in topics:
        value[topic] += 1.0
    return value


class TopicalTests(SimpleTestCase):
    def test_front_and_back_matter_is_not_a_topic(self):
        for title in (
            "Contents",
            "Table of Contents",
            "Index",
            "References",
            "7 References",
            "Bibliography",
            "Acknowledgements",
            "Preface",
            "List of Contributors",
            "Further Reading",
            "Použitá literatúra",
            "Obsah",
            "Literaturverzeichnis",
            "Autorinnen und Autoren",
            "Abkürzungen",
        ):
            self.assertFalse(similarity.is_topical(title), title)

    def test_a_heading_that_only_starts_like_one_is(self):
        for title in (
            "Reference Architecture for Smart Grids",
            "6.5 PID Controller",
            "Indexing Methods",
            "1 Introduction",
        ):
            self.assertTrue(similarity.is_topical(title), title)


class EntryNeighbourTests(SimpleTestCase):
    def setUp(self):
        # Three subjects of three books each, and one generalist close to everything.
        self.vectors = {f"{topic}-{n}": vector(topic, seed=topic * 10 + n) for topic in (1, 2, 3) for n in range(3)}
        self.vectors["generalist"] = vector(1, 2, 3, seed=99)

    def test_raw_cosine_cannot_tell_them_apart(self):
        a, b = self.vectors["1-0"], self.vectors["3-0"]
        self.assertGreater(float(a @ b / np.linalg.norm(a) / np.linalg.norm(b)), 0.7)

    def test_mutual_neighbours_stay_within_a_subject(self):
        pairs = similarity.entry_neighbours(self.vectors, k=2)
        self.assertTrue(pairs)
        for a, b, score in pairs:
            self.assertEqual(a.split("-")[0], b.split("-")[0], (a, b))
            self.assertGreater(score, 0.5)

    def test_the_order_of_the_input_does_not_matter(self):
        reversed_input = dict(reversed(list(self.vectors.items())))
        self.assertEqual(similarity.entry_neighbours(self.vectors), similarity.entry_neighbours(reversed_input))

    def test_too_few(self):
        self.assertEqual(similarity.entry_neighbours({"a": vector(1)}), [])

    def test_a_minority_language_is_centred_by_its_own_mean(self):
        # Six German books (two per subject) share a language component stronger than their subject, as on dev.
        german = np.zeros(EMBEDDING_DIMENSIONS)
        german[500] = 2.0
        vectors = dict(self.vectors)
        del vectors["generalist"]
        languages = {key: "en" for key in vectors}
        for topic in (1, 2, 3):
            for n in range(2):
                vectors[f"{topic}-de{n}"] = vector(topic, seed=500 + topic * 10 + n) + german
                languages[f"{topic}-de{n}"] = "de"

        def subject(key):
            return key.split("-")[0]

        # One mean for all: German books pair with German books of another subject.
        mixed = similarity.entry_neighbours(vectors, k=3)
        self.assertTrue(any(subject(a) != subject(b) for a, b, _s in mixed))
        # Per language: only the subject decides, across the two languages too.
        pairs = similarity.entry_neighbours(vectors, k=3, languages=languages)
        self.assertTrue(pairs)
        self.assertTrue(all(subject(a) == subject(b) for a, b, _s in pairs), pairs)
        self.assertTrue(any(languages[a] != languages[b] for a, b, _s in pairs))

    def test_a_small_language_falls_back_to_the_mean_of_all(self):
        centred = similarity._centre(
            np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]), ["en", "en", "sk"], ["a", "b", "c"]
        )
        # Fewer than MIN_LANGUAGE_BOOKS books everywhere: one mean, exactly as without languages.
        expected = similarity._centre(np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]), [None] * 3, ["a", "b", "c"])
        np.testing.assert_allclose(centred, expected)


#: Three sections of three paragraphs each: two topics and the references.
PARAGRAPHS = [
    (page, section, f"{section} paragraph {page}.")
    for section in ("1 Pumps", "2 Valves", "References")
    for page in (1, 2, 3)
]
TOPICS = {"1 Pumps": 10, "2 Valves": 20, "References": 30}


class SectionSimilarityTests(TestCase):
    def setUp(self):
        reader = fixtures.user()
        catalog = fixtures.catalog(reader)
        self.publications = []
        for n, title in enumerate(("Pumps and Valves", "Hydraulic Machines", "Pipe Networks")):
            entry, run, rows, _f, _s = fixtures.publication(reader, catalog, title=title, paragraphs=PARAGRAPHS)
            for row in rows:
                # The third book has no valves: its second section is about something else.
                topic = TOPICS[row.section] if not (n == 2 and row.section == "2 Valves") else 40
                row.embedding = vector(topic, seed=n * 100 + row.seq)
                row.save(update_fields=["embedding"])
            self.publications.append((entry, run))

    def _pairs(self):
        return {
            frozenset([(str(s.source_id), s.source_section), (str(s.target_id), s.target_section)])
            for s in SectionSimilarity.objects.all()
        }

    def test_same_topic_in_two_books_is_a_pair(self):
        similarity.rebuild()
        pairs = self._pairs()
        (_e0, run0), (_e1, run1), (_e2, run2) = self.publications
        self.assertIn(frozenset([(str(run0.pk), "1 Pumps"), (str(run1.pk), "1 Pumps")]), pairs)
        self.assertIn(frozenset([(str(run0.pk), "2 Valves"), (str(run1.pk), "2 Valves")]), pairs)
        self.assertNotIn(frozenset([(str(run0.pk), "2 Valves"), (str(run2.pk), "2 Valves")]), pairs)
        self.assertFalse(
            SectionSimilarity.objects.filter(source_section="References").exists()
            or SectionSimilarity.objects.filter(target_section="References").exists()
        )
        # Never a pair inside one book.
        self.assertFalse(any(len({key[0] for key in pair}) == 1 for pair in pairs))

    def test_incremental_equals_rebuild(self):
        for _entry, run in self.publications:
            similarity.compute_run(run)
        incremental = self._pairs()
        similarity.rebuild()
        self.assertEqual(self._pairs(), incremental)
        # Repeating a run replaces its pairs, never duplicates them.
        similarity.compute_run(self.publications[0][1])
        self.assertEqual(self._pairs(), incremental)
        self.assertEqual(SectionSimilarity.objects.count(), len(incremental))

    def test_inactive_runs_and_other_models_are_left_out(self):
        (_e0, run0), (_e1, run1), (_e2, run2) = self.publications
        run1.is_active = False
        run1.save(update_fields=["is_active"])
        run2.provenance = {"paragraphs": {"embedding_model": "other@2"}}
        run2.save(update_fields=["provenance"])
        similarity.rebuild()
        self.assertEqual(SectionSimilarity.objects.count(), 0)

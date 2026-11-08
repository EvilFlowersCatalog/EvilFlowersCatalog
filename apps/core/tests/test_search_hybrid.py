"""
IP-021 / #102: hybrid search — Reciprocal Rank Fusion of the lexical and the vector channel, the TEI
client contract, entry embedding, degradation without TEI, and the OPDS `mode=keyword|semantic` channels.

`HybridGoldenTests` evaluates the golden set against the real `tei-embed` and is skipped unless
`EVILFLOWERS_TEST_EMBEDDING_URL` (and `EVILFLOWERS_TEST_EMBEDDING_API_KEY`) are set.
"""

import math
import os
from contextlib import nullcontext
from unittest import mock

import requests
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.search import embeddings, vectors
from apps.core.search.query import Channel, SearchState, rrf
from apps.core.tests.test_search_golden import CORPUS, GOLDEN, SearchFixtureMixin

MODEL = "test-model@1"
DIMENSIONS = 1024


def axis(index: int) -> list:
    """A unit vector along one axis: entries on the same axis are identical in meaning."""
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


def store_vector(entry_id, vector, model=MODEL):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE entries SET embedding = %s::halfvec, embedding_model = %s WHERE id = %s",
            [vectors._literal(vector), model, entry_id],
        )


class RrfTests(SimpleTestCase):
    def test_fuses_by_reciprocal_rank(self):
        fused = rrf([["a", "b", "c"], ["c", "d"]], k=60)
        self.assertEqual([entry_id for entry_id, _ in fused], ["c", "a", "b", "d"])
        self.assertAlmostEqual(dict(fused)["c"], 1 / 63 + 1 / 61)

    def test_single_list_keeps_its_order(self):
        self.assertEqual([entry_id for entry_id, _ in rrf([["x", "y"]], k=60)], ["x", "y"])


@override_settings(EVILFLOWERS_EMBEDDING_URL="http://tei.test", EVILFLOWERS_EMBEDDING_API_KEY="k")
class EmbeddingClientTests(SimpleTestCase):
    def response(self, status=200, payload=None):
        response = mock.Mock(status_code=status, text="")
        response.json.return_value = payload
        return response

    @mock.patch("apps.core.search.embeddings.cache")
    @mock.patch("apps.core.search.embeddings.requests.request")
    def test_query_carries_the_instruction_documents_do_not(self, request, cache):
        cache.get.return_value = None
        request.return_value = self.response(payload=[[0.1, 0.2]])
        with override_settings(EVILFLOWERS_EMBEDDING_MODEL=MODEL):
            embeddings.embed_query("strojové učenie")
            query_input = request.call_args.kwargs["json"]["inputs"][0]
            embeddings.embed_documents(["Strojové učenie v praxi"])
            document_input = request.call_args.kwargs["json"]["inputs"][0]
        self.assertTrue(query_input.startswith("Instruct: Given a library search query"))
        self.assertTrue(query_input.endswith("\nQuery: strojové učenie"))
        self.assertEqual(document_input, "Strojové učenie v praxi")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer k")

    @mock.patch("apps.core.search.embeddings.requests.request")
    def test_documents_go_in_batches_of_32(self, request):
        request.side_effect = lambda *args, **kwargs: self.response(payload=[[0.0]] * len(kwargs["json"]["inputs"]))
        self.assertEqual(len(embeddings.embed_documents([f"t{i}" for i in range(70)])), 70)
        self.assertEqual([len(call.kwargs["json"]["inputs"]) for call in request.call_args_list], [32, 32, 6])

    @mock.patch("apps.core.search.embeddings.time.sleep")
    @mock.patch("apps.core.search.embeddings.requests.request")
    def test_retries_on_overload_then_gives_up(self, request, sleep):
        request.return_value = self.response(status=429)
        with self.assertRaises(embeddings.EmbeddingError):
            embeddings.embed_documents(["x"])
        self.assertEqual(request.call_count, embeddings.RETRIES + 1)

    @mock.patch("apps.core.search.embeddings.requests.request", side_effect=requests.ConnectionError("down"))
    @mock.patch("apps.core.search.embeddings.time.sleep")
    def test_try_embed_query_degrades_to_none(self, sleep, request):
        with override_settings(EVILFLOWERS_EMBEDDING_MODEL=MODEL):
            self.assertIsNone(embeddings.try_embed_query("cokoľvek"))

    @override_settings(EVILFLOWERS_EMBEDDING_HOST="embed.elvira.internal")
    @mock.patch("apps.core.search.embeddings.requests.request")
    def test_host_header_for_the_compose_gateway_path(self, request):
        request.return_value = self.response(payload=[[0.0]])
        embeddings.embed_documents(["x"])
        self.assertEqual(request.call_args.kwargs["headers"]["Host"], "embed.elvira.internal")

    @override_settings(EVILFLOWERS_EMBEDDING_URL="")
    def test_disabled_without_url(self):
        self.assertFalse(embeddings.enabled())
        self.assertIsNone(embeddings.try_embed_query("x"))


class EmbeddingScheduleTests(SearchFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()

    def test_nothing_is_queued_without_tei(self):
        with mock.patch("apps.core.tasks.embed_entries.delay") as delay:
            entry = self.catalog.entries.first()
            entry.title = "Zmenený názov"
            entry.save()
        delay.assert_not_called()

    @override_settings(EVILFLOWERS_EMBEDDING_URL="http://tei.test")
    def test_one_task_per_transaction_after_commit(self):
        entry = self.catalog.entries.get(pk=self.keys["ml"])
        vectors._pending.ids = None  # fixtures of other classes ran in transactions that rolled back
        with mock.patch("apps.core.tasks.embed_entries.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                entry.title = "Machine Learning, 2nd edition"
                entry.save()
                entry.categories.add(self.categories["51"])
        delay.assert_called_once_with([str(entry.pk)])

    @override_settings(EVILFLOWERS_EMBEDDING_URL="http://tei.test", EVILFLOWERS_EMBEDDING_MODEL=MODEL)
    def test_embed_entries_stores_vector_and_model(self):
        entry_id = self.keys["ml"]
        with mock.patch("apps.core.search.embeddings.embed_documents", return_value=[axis(3)]) as embed:
            self.assertEqual(vectors.embed_entries([entry_id]), 1)
        text = embed.call_args.args[0][0]
        self.assertTrue(text.startswith("Machine Learning\nTom Mitchell\n004"))
        self.assertIn("Neural networks", text)
        with connection.cursor() as cursor:
            cursor.execute("SELECT embedding IS NOT NULL, embedding_model FROM entries WHERE id = %s", [entry_id])
            self.assertEqual(cursor.fetchone(), (True, MODEL))


@override_settings(
    EVILFLOWERS_EMBEDDING_URL="http://tei.test",
    EVILFLOWERS_EMBEDDING_MODEL=MODEL,
    EVILFLOWERS_SEARCH_VECTOR_MIN_SCORE=0.5,
)
class HybridRankingTests(SearchFixtureMixin, TestCase):
    """Hand-made vectors: `ml` and `strojove` share an axis, `climate` another; the query lies on the first."""

    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()
        store_vector(cls.keys["ml"], axis(0))
        store_vector(cls.keys["strojove"], axis(0))
        store_vector(cls.keys["climate"], axis(1))
        # A vector of another model is never compared.
        store_vector(cls.keys["rudin"], axis(0), model="other-model@2")

    def search(self, query, vector=axis(0), **params):
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=vector):
            response = self.client.get(
                "/api/v1/entries", {"query": query, "catalog_id": str(self.catalog.pk), **params}
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def ids(self, payload):
        return [item["id"] for item in payload["items"]]

    def test_semantic_neighbours_join_the_lexical_hits(self):
        payload = self.search("machine learning")
        self.assertEqual(payload["metadata"]["search"]["mode"], "hybrid")
        ids = self.ids(payload)
        # lexical + vector hit first, then the vector-only neighbour
        self.assertEqual(ids[:2], [str(self.keys["ml"]), str(self.keys["strojove"])])
        self.assertNotIn(str(self.keys["rudin"]), ids)
        neighbour = next(item for item in payload["items"] if item["id"] == str(self.keys["strojove"]))
        self.assertEqual(neighbour["search"]["matched"], ["semantic"])

    def test_words_nobody_contains_return_semantic_neighbours(self):
        payload = self.search("umelá inteligencia")
        self.assertEqual(set(self.ids(payload)), {str(self.keys["ml"]), str(self.keys["strojove"])})
        self.assertEqual(payload["metadata"]["search"]["fallback"], "semantic")
        self.assertTrue(payload["metadata"]["search"]["relaxed"])

    def test_typo_still_reaches_the_lexical_fallback(self):
        payload = self.search("Hirshberger", vector=axis(7))
        self.assertEqual(payload["metadata"]["search"]["fallback"], "similar")
        self.assertEqual(self.ids(payload)[0], str(self.keys["philosophie"]))

    def test_tei_down_degrades_to_lexical(self):
        payload = self.search("machine learning", vector=None)
        self.assertIn(payload["metadata"]["search"]["mode"], ("fulltext", "bm25"))
        self.assertEqual(self.ids(payload), [str(self.keys["ml"])])

    def test_tei_info_down_degrades_instead_of_failing(self):
        with mock.patch(
            "apps.core.search.embeddings.model_id", side_effect=embeddings.EmbeddingError("TEI /info down")
        ):
            payload = self.search("machine learning")
        self.assertTrue(payload["metadata"]["search"]["degraded"])
        self.assertEqual(self.ids(payload), [str(self.keys["ml"])])

    def test_filters_apply_to_both_channels(self):
        payload = self.search("machine learning", language_code="sk")
        self.assertEqual(self.ids(payload), [str(self.keys["strojove"])])

    def test_opds2_semantic_and_keyword_modes(self):
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=axis(0)):
            semantic = self.client.get("/opds/v2/open/search", {"query": "umelá inteligencia", "mode": "semantic"})
            keyword = self.client.get("/opds/v2/open/search", {"query": "umelá inteligencia", "mode": "keyword"})
        self.assertEqual(semantic.status_code, 200, semantic.content[:300])
        self.assertEqual(semantic.json()["metadata"]["numberOfItems"], 2)
        self.assertEqual(keyword.json()["metadata"]["numberOfItems"], 0)

    def test_similar_entries_by_entry_vector(self):
        response = self.client.get(f"/api/v1/entries/{self.keys['ml']}/similar")
        self.assertEqual(response.status_code, 200, response.content[:300])
        similar = response.json()["response"]
        self.assertEqual(similar[0]["entry_id"], str(self.keys["strojove"]))
        self.assertNotIn(str(self.keys["rudin"]), [row["entry_id"] for row in similar])

    def test_state_channels(self):
        state = SearchState("x", channels=(Channel.VECTOR,))
        with mock.patch("apps.core.search.embeddings.try_embed_query", return_value=axis(0)):
            self.assertEqual(state.mode, "semantic")


# Queries that share no word with their target: only the vector channel can find them.
SEMANTIC_GOLDEN = [
    ("artificial intelligence", ["ml", "strojove"]),
    ("umelá inteligencia", ["strojove", "ml"]),
    ("teaching adults", ["vzdelavanie"]),
    ("history of Slovakia", ["dejiny"]),
    ("animal behaviour", ["buecher"]),
    ("company management and finance", ["ekonomika"]),
    ("derivatives and integrals", ["salat", "rudin"]),
    ("public libraries", ["kniznice"]),
]


def ndcg_at_10(ranked: list, relevant: list) -> float:
    gains = {entry_id: 1.0 for entry_id in relevant}
    dcg = sum(gains.get(entry_id, 0.0) / math.log2(position + 1) for position, entry_id in enumerate(ranked[:10], 1))
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(relevant), 10) + 1))
    return dcg / ideal if ideal else 0.0


TEST_TEI_URL = os.environ.get("EVILFLOWERS_TEST_EMBEDDING_URL", "")


@override_settings(
    EVILFLOWERS_EMBEDDING_URL=TEST_TEI_URL,
    EVILFLOWERS_EMBEDDING_API_KEY=os.environ.get("EVILFLOWERS_TEST_EMBEDDING_API_KEY", ""),
    EVILFLOWERS_EMBEDDING_MODEL="",
    # Calibration: EVILFLOWERS_TEST_VECTOR_MIN_SCORE overrides the production default for one run.
    **(
        {"EVILFLOWERS_SEARCH_VECTOR_MIN_SCORE": float(os.environ["EVILFLOWERS_TEST_VECTOR_MIN_SCORE"])}
        if os.environ.get("EVILFLOWERS_TEST_VECTOR_MIN_SCORE")
        else {}
    ),
    **(
        {"EVILFLOWERS_SEARCH_VECTOR_WEIGHT": float(os.environ["EVILFLOWERS_TEST_VECTOR_WEIGHT"])}
        if os.environ.get("EVILFLOWERS_TEST_VECTOR_WEIGHT")
        else {}
    ),
)
class HybridGoldenTests(SearchFixtureMixin, TestCase):
    """The golden set with real e5 vectors: nDCG@10 per language, lexical vs hybrid."""

    @classmethod
    def setUpClass(cls):
        if not TEST_TEI_URL:
            raise __import__("unittest").SkipTest("EVILFLOWERS_TEST_EMBEDDING_URL not set")
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()
        vectors.embed_entries(cls.keys.values())

    def ranked(self, query: str, hybrid: bool) -> list:
        # Lexical: the query is not embedded. Hybrid: the real TEI embeds it.
        patcher = (
            nullcontext() if hybrid else mock.patch("apps.core.search.embeddings.try_embed_query", return_value=None)
        )
        with patcher:
            response = self.client.get(
                "/api/v1/entries", {"query": query, "catalog_id": str(self.catalog.pk), "limit": 50}
            )
        names = {str(pk): key for key, pk in self.keys.items()}
        return [names[item["id"]] for item in response.json()["items"]]

    def test_hybrid_beats_lexical(self):
        languages = {key: language for key, (_t, language, *_rest) in CORPUS.items()}
        report, totals, worse = [], {}, []
        for label, golden in (("golden", GOLDEN), ("semantic", SEMANTIC_GOLDEN)):
            for query, relevant in golden:
                language = languages[relevant[0]]
                scores = {}
                for hybrid in (False, True):
                    ranked = self.ranked(query, hybrid)
                    scores[hybrid] = ndcg_at_10(ranked, relevant)
                    totals.setdefault((label, language, hybrid), []).append(scores[hybrid])
                if scores[True] < scores[False]:
                    worse.append(f"  {query!r}: expected {relevant}, hybrid {ranked[:5]}")
        for (label, language, hybrid), scores in sorted(totals.items()):
            report.append(
                f"  {label:8s} {language}  {'hybrid ' if hybrid else 'lexical'}  nDCG@10 {sum(scores) / len(scores):.3f}"
                f"  ({len(scores)} queries)"
            )
        if worse:
            report.append("Worse with vectors:\n" + "\n".join(worse))
        print("\nHybrid golden set (multilingual-e5-large-instruct):\n" + "\n".join(report))

        def mean(label, language, hybrid):
            scores = totals.get((label, language, hybrid), [])
            return sum(scores) / len(scores) if scores else 0.0

        for language in ("sk", "en"):
            self.assertGreaterEqual(mean("golden", language, True), mean("golden", language, False) - 0.02)
            self.assertGreater(
                mean("semantic", language, True) + mean("golden", language, True),
                mean("semantic", language, False) + mean("golden", language, False),
            )

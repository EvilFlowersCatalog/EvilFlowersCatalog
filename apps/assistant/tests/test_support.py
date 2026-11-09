"""Support check (#117) and the retrieval benchmark (#118)."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.assistant import evaluation, retrieval_bench, support
from apps.assistant.evidence import Ledger
from apps.assistant.llm import Delta, LLMUnavailable


class SplitSentencesTests(SimpleTestCase):
    def test_abbreviations_initials_and_decimals_do_not_split(self):
        text = (
            "Kniha J. Nováka opisuje napr. tretiu normálnu formu [E1]. Hodnota 3.5 je hranica, t. j. limit [E2]. "
            "See e.g. chapter 2 in Smith et al. for details. Is it clear? Yes!"
        )
        self.assertEqual(
            support.split_sentences(text),
            [
                "Kniha J. Nováka opisuje napr. tretiu normálnu formu [E1].",
                "Hodnota 3.5 je hranica, t. j. limit [E2].",
                "See e.g. chapter 2 in Smith et al. for details.",
                "Is it clear?",
                "Yes!",
            ],
        )

    def test_a_label_after_the_full_stop_belongs_to_the_sentence_before(self):
        self.assertEqual(
            support.split_sentences("Pressure falls. [E2] Speed rises. [E3][E4]"),
            ["Pressure falls. [E2]", "Speed rises. [E3][E4]"],
        )

    def test_empty(self):
        self.assertEqual(support.split_sentences("  "), [])


class Verifier:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def chat(self, messages, tools=None, response_format=None):
        self.requests.append({"messages": messages, "tools": tools, "format": response_format})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        yield Delta(content=answer)
        yield Delta(done=True, prompt_tokens=300, completion_tokens=20)


LEDGER = Ledger(
    {"E1": {"entry_id": "a", "page": 4}, "E2": {"entry_id": "b", "page": 9}},
    {"E1": "Bernoulli: pressure falls where speed rises.", "E2": "Viscosity resists flow."},
)


class SupportCheckTests(SimpleTestCase):
    def test_pairs_carry_context_and_only_known_passages(self):
        pairs = support.pairs("Intro. Pressure falls as speed rises [E1, E2]. Unknown [E7]. End.", LEDGER)
        self.assertEqual([pair.label for pair in pairs], ["E1", "E2"])
        self.assertEqual(pairs[0].context, "Intro. Unknown [E7].")

    def test_verdicts_constrained_and_unverified_on_failure(self):
        client = Verifier(
            [
                '{"verdict": "supported", "score": 91, "reason": "states it"}',
                "not json at all",
                LLMUnavailable("down"),
                '{"verdict": "partial", "score": 55}',
            ]
        )
        verdicts = support.verify("Pressure falls [E1]. Viscosity resists flow [E2]. Again [E1, E2].", LEDGER, client)
        # Sentence 3 cites E1 and E2 again: four pairs. Unusable JSON and a failed call are `unverified`.
        self.assertEqual([v.verdict for v in verdicts], ["supported", "unverified", "unverified", "partial"])
        self.assertEqual(verdicts[0].score, 91)
        self.assertEqual(verdicts[0].page, 4)
        self.assertEqual(client.requests[0]["format"], support.SUPPORT_SCHEMA)
        self.assertIsNone(client.requests[0]["tools"])

    def test_calibration_table(self):
        rows = support.calibrate([(True, 90), (True, 40), (False, 70), (False, None)], thresholds=[50, 80])
        self.assertEqual(rows[0], {"threshold": 50, "passed": 2, "precision": 0.5, "recall": 0.5})
        self.assertEqual(rows[1], {"threshold": 80, "passed": 1, "precision": 1.0, "recall": 0.5})

    def test_calibrate_splits_tuning_and_held_out_halves(self):
        labelled = [{"id": f"p{n}", "sentence": "s", "passage": "p", "supported": n % 2 == 0} for n in range(20)] + [
            {"id": "unlabelled", "sentence": "s", "passage": "p", "supported": None}
        ]
        client = Verifier(['{"verdict": "supported", "score": 80}'] * 20)
        result = evaluation.calibrate(labelled, threshold=50, client=client)
        self.assertEqual(result["tuning_pairs"] + result["held_out_pairs"], 20)
        self.assertEqual(result["unlabelled"], 1)
        self.assertEqual(result["held_out"]["threshold"], 50)
        # The split is fixed by id, so a threshold chosen on one half is tested on the other.
        self.assertEqual(evaluation.held_out("p3"), evaluation.held_out("p3"))


class RetrievalBenchTests(SimpleTestCase):
    CASE = {"id": "x", "question": "q", "gold": [{"entry_id": "a", "pages": [12, 13]}, {"entry_id": "c"}]}

    def test_metrics(self):
        self.assertEqual(retrieval_bench.recall_at(["b", "a"], {"a", "c"}, 5), 0.5)
        self.assertAlmostEqual(retrieval_bench.ndcg_at(["a"], {"a"}, 10), 1.0)
        self.assertAlmostEqual(retrieval_bench.ndcg_at(["b", "a"], {"a"}, 10), 1 / 1.584962500721156)
        self.assertEqual(retrieval_bench.reciprocal_rank(["b", "c"], {"c"}), 0.5)

    def test_rankings_order_passages_by_score(self):
        result = SimpleNamespace(
            items=[
                SimpleNamespace(entry_id="b", passages=[SimpleNamespace(score=0.9, page=1)]),
                SimpleNamespace(
                    entry_id="a", passages=[SimpleNamespace(score=0.8, page=12), SimpleNamespace(score=0.95, page=40)]
                ),
            ]
        )
        entries, passages = retrieval_bench.rankings(result)
        self.assertEqual(entries, ["b", "a"])
        self.assertEqual(passages, [("a", 40), ("b", 1), ("a", 12)])

        scored = retrieval_bench.score_case(self.CASE, entries, passages)
        self.assertEqual(scored.entry_recall_5, 0.5)
        self.assertEqual(scored.entry_rr, 0.5)
        self.assertEqual(scored.page_rr, 1 / 3)
        self.assertTrue(scored.has_pages)

    def test_report_marks_preregistered_bars(self):
        result = retrieval_bench.score_case(self.CASE, ["a"], [("a", 12)])
        report = retrieval_bench.report([result], {"entry_recall_5": 0.4, "page_recall_5": 0.9})
        self.assertTrue(report["thresholds"]["entry_recall_5"]["met"])
        self.assertFalse(report["thresholds"]["page_recall_5"]["met"])

    def test_gold_questions_become_eval_cases_that_must_cite_a_gold_publication(self):
        (case,) = retrieval_bench.as_eval_cases([self.CASE])
        self.assertEqual(case["expect"]["cites_any_entry"], ["a", "c"])
        outcome = evaluation.Outcome(
            id="x", question="q", tools=["search_library_text"], citations=1, cited_entries=["b"]
        )
        self.assertIn("no gold publication cited", evaluation.score(case, outcome).failures)

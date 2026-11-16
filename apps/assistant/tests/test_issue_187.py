"""#187: the evaluation under prefetch, the language pin, and what is not looked up before answering."""

from django.test import SimpleTestCase

from apps.assistant import evaluation, language, pack, retrieval_bench, services
from apps.extraction.bibliography import looks_like_references

REFERENCES = (
    "Abadi, M., Barham, P. (2016). TensorFlow: A system for large-scale machine learning. In OSDI, 265-283. "
    "He, K., Zhang, X., Ren, S. (2016). Deep residual learning for image recognition. In CVPR. "
    "LeCun, Y., Bengio, Y., Hinton, G. (2015). Deep learning. Nature 521, 436-444. "
    "Silver, D. et al. (2017). Mastering the game of Go without human knowledge. Nature 550, 354-359."
)
PROSE = (
    "Tobacco taxes as a tobacco control strategy are one of the most potent instruments for reducing consumption "
    "(World Bank 1999). Raising the price lowers smoking among young people in particular, and the Dutch "
    "government used them from the 1990s on, although the revenue argument often weighed more than health."
)


class ReferenceListTests(SimpleTestCase):
    """Passage search leaves reference lists out: they match every topic they list and support no claim."""

    def test_dense_years_or_a_reference_heading(self):
        self.assertTrue(looks_like_references(REFERENCES))
        self.assertTrue(looks_like_references("Anything at all.", "References"))
        self.assertTrue(looks_like_references("See doi:10.1000/182 and 10.1038/nature14539 for details."))

    def test_prose_citing_a_source_stays(self):
        self.assertFalse(looks_like_references(PROSE, "2 Tobacco taxes"))
        self.assertFalse(looks_like_references("", None))


class LanguageTests(SimpleTestCase):
    def test_guess_by_weight(self):
        self.assertEqual(language.guess("Recommend a book about operating systems."), "en")
        self.assertEqual(language.guess("What did I save on my shelf?"), "en")
        self.assertEqual(language.guess("Máte nejaké knihy o strojovom učení?"), "sk")
        self.assertEqual(language.guess("Potřebuji knihu o statistice, máte něco?"), "cs")
        self.assertIsNone(language.guess("ok"))
        # Slovak typed without diacritics (real dev questions the export had called English).
        self.assertEqual(language.guess("vies mi zistit co sa pise o patchoch?"), "sk")
        self.assertEqual(language.guess("vysvetli mi podla knihy rfid. vies mi ukazat obrazky?"), "sk")
        # A name with an accent does not make English Slovak (`en-showcase`).
        english = "I am Elvíra, your digital library assistant. Here are some of the things I can do for you."
        self.assertEqual(language.guess(english), "en")

    def test_answer_language_answers_czech_in_slovak_and_falls_back_to_the_ui(self):
        self.assertEqual(language.answer_language("Potřebuji knihu o statistice, máte něco?", "en"), "sk")
        self.assertEqual(language.answer_language("Recommend a book about operating systems.", "sk"), "en")
        self.assertEqual(language.answer_language("ok", "sk-SK"), "sk")
        self.assertIsNone(language.answer_language("ok", None))

    def test_the_pin_names_the_language_of_the_message(self):
        self.assertTrue(
            services.language_instruction("sk", "Recommend a book about operating systems.").startswith(
                "Answer in English"
            )
        )
        czech = services.language_instruction("en", "Potřebuji knihu o statistice, máte něco?")
        self.assertTrue(czech.startswith("Answer in Slovak"))
        self.assertIn("never Czech", czech)
        self.assertTrue(services.language_instruction("sk", "ok").startswith("Answer in Slovak"))
        # No message and no UI language: the generic rule, as before.
        self.assertIn("language of the reader's last message", services.language_instruction(None, "ok"))
        self.assertIn("language of the reader's last message", services.language_instruction("sk"))

    def test_the_eval_scores_an_english_answer_with_a_name_as_english(self):
        answer = "I am Elvíra, your digital library assistant. I can search the catalog and show figures."
        self.assertEqual(evaluation.answer_language(answer), "en")
        self.assertEqual(
            evaluation.answer_language("V digitální knihovně nemám samostatnou knihu, ale mám několik."), "cs"
        )


class NeedsPackTests(SimpleTestCase):
    def test_dedicated_tools_are_not_preempted_by_a_pack(self):
        for text in (
            "What did I save on my shelf?",
            "Čo mám momentálne požičané a kedy mi to vyprší?",
            "Kde nájdem svoje výpožičky? Ukáž mi to.",
            "Kde nájdem úlohy OCR, ktoré zlyhali? Ukáž mi to.",
            "Give me a proper showcase of the portal. Can you navigate me?",
            "Z akých oblastí máte knihy?",
        ):
            self.assertFalse(pack.needs_pack(text), text)

    def test_study_requests_only_in_a_chat_about_a_publication(self):
        for text in (
            "I need to learn this for a test. Quiz me on the first chapter.",
            "Aké sú kľúčové pojmy prvej kapitoly?",
            "Zhrň mi úvodnú kapitolu.",
            "Musím sa to do rána naučiť, prejdi so mnou túto knihu po kapitolách.",
        ):
            self.assertFalse(pack.needs_pack(text, about_publication=True), text)
        self.assertTrue(pack.needs_pack("Summarize what the library says about Bernoulli's equation."))

    def test_content_questions_still_get_one(self):
        for text in (
            "Ako funguje Bernoulliho rovnica?",
            "Ktoré knihy o databázach si môžem požičať hneď teraz?",
            "What does the library say about tobacco control policy in the Netherlands?",
        ):
            self.assertTrue(pack.needs_pack(text), text)


class PrefetchScoringTests(SimpleTestCase):
    def _score(self, expect, tools):
        return evaluation.score(
            {"id": "c", "question": "q", "expect": expect}, evaluation.Outcome(id="c", question="q", tools=tools)
        )

    def test_the_pack_counts_as_the_searches_it_runs(self):
        self.assertTrue(self._score({"tools_any": ["search_entries"]}, ["gather_evidence"]).passed)
        self.assertTrue(self._score({"tools_all": ["search_entries"]}, ["gather_evidence"]).passed)
        self.assertTrue(
            self._score({"tools_any": ["search_library_text", "read_pdf_page"]}, ["gather_evidence"]).passed
        )

    def test_but_not_as_narrower_tools_nor_as_a_forbidden_call(self):
        self.assertFalse(self._score({"tools_all": ["get_entry"]}, ["gather_evidence"]).passed)
        self.assertFalse(self._score({"tools_any": ["list_categories"]}, ["gather_evidence"]).passed)
        self.assertTrue(self._score({"tools_none": ["search_library_text"]}, ["gather_evidence"]).passed)

    def test_gold_cases_need_reading_not_one_tool(self):
        cases = retrieval_bench.as_eval_cases(
            [{"id": "x", "question": "q", "gold": [{"entry_id": "e", "pages": [3]}]}]
        )
        self.assertEqual(cases[0]["expect"], {"grounded": True, "cites": True, "cites_any_entry": ["e"]})
        self.assertIn("gather_evidence", evaluation.TEXT_TOOLS)

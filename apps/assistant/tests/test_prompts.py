"""Prompt templates (#116)."""

import hashlib
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from apps.assistant import explain, highlight, prompts, semantic, services, tools

#: sha256 of each system prompt as it was a Python constant, before the switch to templates. Moving the text into
#: files must not change one byte (the cached prompt prefix and every evaluation so far depend on it).
#: When a prompt is changed on purpose, update its hash here and record the `assistant_eval` run that justified it.
PROMPT_HASHES = {
    # 2026-11-11, #127/#130: reading publications by id, grounding, two-searches stop, citation placement.
    # Written from the first dev run (#104), NOT yet re-run on elvira-assistant: that run is the follow-up of #130.
    "chat": "38ec49e52affe2e434476ba421e6e0ec39b552c32a4531a000985ecf571f78ca",
    "explain": "78c31a0ef64cd0cd56c2d2b573c7967fe15bfa7632836fda6a0e741599542120",
    "highlight": "e18e507f1b61be651fabf82ea6bebe13a11f95de93c9183f63d8150127805369",
    "semantic": "58e58ff31be28adb45ea2c6890169af7c3e508461df8d1648b7265ae8355d153",
}


class PromptTemplateTests(SimpleTestCase):
    def test_rendered_prompts_are_unchanged(self):
        for name, module in (
            ("chat", services),
            ("explain", explain),
            ("highlight", highlight),
            ("semantic", semantic),
        ):
            with self.subTest(prompt=name):
                self.assertEqual(hashlib.sha256(module.SYSTEM_PROMPT.encode()).hexdigest(), PROMPT_HASHES[name])

    def test_tool_names_come_from_the_code(self):
        self.assertIn(f"`{tools.SEARCH_PASSAGES}`", services.SYSTEM_PROMPT)
        self.assertNotIn("{{", services.SYSTEM_PROMPT)

    def test_unknown_placeholder_raises(self):
        with self.assertRaises(prompts.PromptError):
            prompts.render("chat")

    def test_override_directory_replaces_one_file_and_falls_back_for_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "semantic.md").write_text("Overridden.\n", encoding="utf-8")
            with override_settings(EVILFLOWERS_ASSISTANT_PROMPTS_DIR=directory):
                self.assertEqual(prompts.render("semantic"), "Overridden.")
                self.assertEqual(
                    prompts.render("explain", search_paragraphs="a", search_pdf="b", read_pdf_page="c")[:11],
                    "You explain",
                )

    def test_missing_template_raises(self):
        with self.assertRaises(prompts.PromptError):
            prompts.load("nope")

    def test_fingerprint_is_stable_and_sensitive(self):
        specs = tools.specifications(authenticated=True)
        first = prompts.fingerprint(services.SYSTEM_PROMPT, specs)
        self.assertEqual(first, prompts.fingerprint(services.SYSTEM_PROMPT, tools.specifications(authenticated=True)))
        self.assertNotEqual(
            first, prompts.fingerprint(services.SYSTEM_PROMPT, tools.specifications(authenticated=False))
        )
        self.assertEqual(len(first), 12)

    def test_language_rule(self):
        self.assertTrue(services.language_instruction("sk-SK").endswith(prompts.NOT_CZECH))
        self.assertNotIn("Czech", services.language_instruction("en"))
        self.assertIsNone(services.language_instruction("de"))
        self.assertTrue(explain._language_instruction("sk").endswith(prompts.NOT_CZECH))

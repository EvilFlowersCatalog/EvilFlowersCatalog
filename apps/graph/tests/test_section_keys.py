"""#171: a section's key does not depend on the run, so links and graph keys survive a re-extraction."""

from django.test import TestCase

from apps.assistant.tests import fixtures
from apps.extraction.tests.test_locators import reextract
from apps.graph import projection
from apps.graph.views import chapter_map


class SectionKeyStabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reader = fixtures.user()
        cls.open = fixtures.catalog(cls.reader)
        cls.entry, cls.extraction, *_ = fixtures.publication(cls.reader, cls.open)

    def test_chapter_keys_are_equal_before_and_after_a_reextraction(self):
        _etag, before = chapter_map(self.entry.pk)
        new = reextract(self.extraction)
        _etag, after = chapter_map(self.entry.pk)
        self.assertNotEqual(new.pk, self.extraction.pk)
        self.assertEqual([s["key"] for s in before["sections"]], [s["key"] for s in after["sections"]])
        self.assertEqual(before["sections"][1]["key"], projection.section_key(self.entry.pk, "2 Bernoulli"))

    def test_keys_differ_between_publications(self):
        other, *_ = fixtures.publication(self.reader, self.open, title="Hydraulics")
        self.assertNotEqual(
            projection.section_key(self.entry.pk, "2 Bernoulli"), projection.section_key(other.pk, "2 Bernoulli")
        )

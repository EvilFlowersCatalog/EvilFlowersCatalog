"""Exact-identifier matching (IP-034 Q1): normalised on both sides, single hit only, system-key collisions."""

from apps.harvest.matching import MatchIndex
from apps.harvest.records import SourceRecord
from apps.harvest.tests.base import HarvestTestCase


class MatchIndexTests(HarvestTestCase):
    def index(self):
        return MatchIndex(self.catalog, "arl_stu")

    def test_hyphenated_stored_isbn13_matches_a_normalised_record_isbn(self):
        entry = self.entry("A", identifiers={"isbn": "978-80-227-1234-7"})
        match = self.index().match(SourceRecord(record_id="1", identifiers={"isbn": "9788022712347"}))
        self.assertEqual((match.status, match.entry_id, match.matched_on), ("matched", str(entry.pk), "isbn"))

    def test_stored_isbn10_matches_the_record_isbn13(self):
        entry = self.entry("B", identifiers={"isbn": "80-227-5078-6"})
        match = self.index().match(SourceRecord(record_id="1", identifiers={"isbn": "9788022750783"}))
        self.assertEqual(match.entry_id, str(entry.pk))

    def test_further_isbns_of_the_record_match_too(self):
        entry = self.entry("C", identifiers={"isbn": "9788022740005"})
        record = SourceRecord(record_id="1", identifiers={"isbn": "9788022712347"}, other_isbns=["9788022740005"])
        self.assertEqual(self.index().match(record).entry_id, str(entry.pk))

    def test_two_entries_with_the_isbn_are_ambiguous(self):
        self.entry("D1", identifiers={"isbn": "9788022712347"})
        self.entry("D2", identifiers={"isbn": "80-227-1234-5"})
        match = self.index().match(SourceRecord(record_id="1", identifiers={"isbn": "9788022712347"}))
        self.assertEqual(match.status, "ambiguous")

    def test_no_identifier_hit_is_unmatched(self):
        self.entry("E", identifiers={"isbn": "9788022712347"})
        match = self.index().match(SourceRecord(record_id="1", identifiers={"isbn": "9788022766661"}))
        self.assertEqual(match.status, "unmatched")

    def test_system_key_wins_over_identifiers(self):
        linked = self.entry("F1", identifiers={"arl_stu": "0000101"})
        self.entry("F2", identifiers={"isbn": "9788022712347"})
        match = self.index().match(SourceRecord(record_id="0000101", identifiers={"isbn": "9788022712347"}))
        self.assertEqual((match.status, match.entry_id, match.matched_on), ("matched", str(linked.pk), "arl_stu"))

    def test_entry_linked_to_another_record_is_ambiguous(self):
        self.entry("G", identifiers={"isbn": "9788022712347", "arl_stu": "0000999"})
        match = self.index().match(SourceRecord(record_id="0000101", identifiers={"isbn": "9788022712347"}))
        self.assertEqual(match.status, "ambiguous")
        self.assertIn("0000999", match.message)

    def test_two_entries_holding_the_record_id_are_ambiguous(self):
        self.entry("H1", identifiers={"arl_stu": "0000101"})
        self.entry("H2", identifiers={"arl_stu": "0000101"})
        self.assertEqual(self.index().match(SourceRecord(record_id="0000101")).status, "ambiguous")

    def test_isbn_decides_before_issn(self):
        # A series ISSN is shared by every volume; the ISBN is the book's.
        book = self.entry("I1", identifiers={"isbn": "9788022712347", "issn": "1335-2016"})
        self.entry("I2", identifiers={"issn": "1335-2016"})
        record = SourceRecord(record_id="1", identifiers={"isbn": "9788022712347", "issn": "1335-2016"})
        self.assertEqual(self.index().match(record).entry_id, str(book.pk))

    def test_entries_of_other_catalogs_are_not_candidates(self):
        from apps.core.models import Entry

        Entry.objects.create(
            creator=self.superuser, catalog=self.other_catalog, title="X", identifiers={"isbn": "9788022712347"}
        )
        match = self.index().match(SourceRecord(record_id="1", identifiers={"isbn": "9788022712347"}))
        self.assertEqual(match.status, "unmatched")

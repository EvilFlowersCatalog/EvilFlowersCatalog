"""Harvest runs end to end (IP-034): modes, field policy, dry run, idempotency, deletions, subjects."""

from unittest.mock import patch

from apps.core.models import Category, Entry
from apps.harvest.matching import SourceBusy
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.records import UDC_SCHEME, SourceAuthor, SourceRecord, SourceSubject
from apps.harvest.runner import run_source
from apps.harvest.tests.base import ARL_PAGES, FakeSession, HarvestTestCase

SESSION = "apps.harvest.adapters.oai.requests.Session"
# Search indexing and extraction are not what is tested here.
QUIET = ("apps.extraction.services.queue",)


class ListAdapter:
    """Records given in the test; the adapter shape the runner needs."""

    exhaustive = False

    def __init__(self, records, cursor=None, exhaustive=False):
        self._records = records
        self.cursor = cursor
        self.exhaustive = exhaustive

    def records(self):
        yield from self._records


def book(record_id="R1", **kwargs) -> SourceRecord:
    values = {
        "record_id": record_id,
        "title": "Kniha",
        "identifiers": {"isbn": "9788022712347"},
        "language": "sk",
        "authors": [SourceAuthor("Macák", "Marek")],
    }
    values.update(kwargs)
    return SourceRecord(**values)


class OaiRunTests(HarvestTestCase):
    def setUp(self):
        self.session = FakeSession(ARL_PAGES)
        patcher = patch(SESSION, return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arl_catalog(self):
        self.e101 = self.entry("Numerická matematika", identifiers={"isbn": "978-80-227-1234-7"})
        self.e102 = self.entry("Applied Mechanics", identifiers={"isbn": "80-227-5078-6"}, language=self.en)
        self.e099 = self.entry("Withdrawn at ARL", identifiers={"arl_stu": "0000099"})

    def test_link_only_sets_the_record_id_and_nothing_else(self):
        self.arl_catalog()
        source = self.source()
        HarvestRecord.objects.create(
            source=source,
            record_id="0000099",
            status=HarvestRecord.Status.MATCHED,
            entry=self.e099,
            source_record={"extra": {"oai_identifier": "oai:kis.example.org:STU_US_CAT*0000099"}},
        )

        report = run_source(source)

        self.e101.refresh_from_db()
        self.e102.refresh_from_db()
        self.e099.refresh_from_db()
        self.assertEqual(self.e101.identifiers, {"isbn": "978-80-227-1234-7", "arl_stu": "0000101"})
        self.assertEqual(self.e102.identifiers, {"isbn": "80-227-5078-6", "arl_stu": "0000102"})
        self.assertEqual(self.e101.title, "Numerická matematika")  # untouched
        self.assertEqual(self.e101.authors.count(), 0)
        # Deleted at source (header with the OAI identifier only): key removed, entry stays.
        self.assertEqual(self.e099.identifiers, {})
        self.assertEqual(
            (report["seen"], report["matched"], report["unmatched"], report["deleted"]), (4, 2, 1, 1), report
        )
        source.refresh_from_db()
        self.assertEqual(source.cursor, "2026-11-16T10:00:05Z")
        self.assertIsNotNone(source.last_run_at)
        rows = {row.record_id: row for row in HarvestRecord.objects.filter(source=source)}
        self.assertEqual(rows["0000101"].matched_on, "isbn")
        self.assertEqual(rows["0000103"].status, "unmatched")
        self.assertEqual(rows["0000099"].status, "deleted")
        self.assertEqual(Entry.objects.filter(catalog=self.catalog).count(), 3)

    def test_second_run_changes_nothing(self):
        self.arl_catalog()
        source = self.source()
        run_source(source)
        before = {e.pk: (e.updated_at, dict(e.identifiers or {})) for e in Entry.objects.all()}

        source.refresh_from_db()
        report = run_source(source)

        after = {e.pk: (e.updated_at, dict(e.identifiers or {})) for e in Entry.objects.all()}
        self.assertEqual(before, after)
        self.assertEqual(report["unchanged"], 2)
        self.assertEqual(report["matched"] + report["enriched"] + report["created"], 0)
        # The second request starts from the cursor.
        self.assertEqual(self.session.calls[-2].get("from"), "2026-11-16T10:00:05Z")

    def test_create_mode_imports_unmatched_records(self):
        self.arl_catalog()
        source = self.source(mode=HarvestSource.Mode.CREATE)

        report = run_source(source)

        self.assertEqual(report["created"], 1, report)
        created = Entry.objects.get(identifiers__arl_stu="0000103")
        self.assertEqual(created.origin, "harvest:arl-stu")
        self.assertEqual(created.title, "Kniha, ktorú katalóg ešte nemá")
        self.assertEqual(created.identifiers["isbn"], "9788022766661")
        self.assertEqual(created.language, self.sk)
        self.assertEqual(created.summary, "")
        self.assertEqual([(a.surname, a.name) for a in created.authors.all()], [("Novák", "Ján")])
        self.assertEqual(created.creator, self.superuser)

    def test_enrich_fills_empty_fields_and_keeps_filled_ones(self):
        self.arl_catalog()
        source = self.source(mode=HarvestSource.Mode.ENRICH)

        run_source(source)

        self.e101.refresh_from_db()
        self.assertEqual(self.e101.title, "Numerická matematika")  # filled: kept
        self.assertEqual(self.e101.publisher, "Spektrum STU")
        self.assertEqual(self.e101.edition, "2. vydanie")
        self.assertEqual(self.e101.page_count, 245)
        self.assertEqual(str(self.e101.published_at), "2018")
        # The stored spelling of the ISBN stays; missing identifiers are added.
        self.assertEqual(self.e101.identifiers["isbn"], "978-80-227-1234-7")
        self.assertEqual(self.e101.identifiers["issn"], "1335-2016")
        self.assertEqual(self.e101.identifiers["ccnb"], "cnb000123456")
        self.assertEqual(
            [(a.surname, a.name) for a in self.e101.authors.order_by("entry_authors__position")],
            [("Macák", "Marek"), ("Minarechová", "Zuzana")],
        )
        # Q3: every subject becomes a category with its scheme.
        categories = {(c.scheme, c.term) for c in self.e101.categories.all()}
        self.assertEqual(
            categories, {(UDC_SCHEME, "519.6"), ("arl-stu", "numerická matematika"), ("arl-stu", "učebnice")}
        )
        # 856 links are opt-in.
        self.assertFalse(self.e101.acquisitions.exists())
        self.assertEqual(HarvestRecord.objects.get(source=source, record_id="0000101").status, "enriched")

    def test_field_policy_overwrite_and_ignore(self):
        self.arl_catalog()
        source = self.source(
            mode=HarvestSource.Mode.ENRICH,
            field_policy={"title": "overwrite", "publisher": "ignore", "links": "fill_empty"},
        )

        run_source(source)

        self.e101.refresh_from_db()
        self.assertEqual(self.e101.title, "Numerická matematika: vybrané kapitoly")
        self.assertIsNone(self.e101.publisher)
        acquisition = self.e101.acquisitions.get()
        self.assertEqual(acquisition.file_url, "https://files.example.org/stu/0000101.pdf")
        self.assertEqual(acquisition.storage_backend, "external_url")

    def test_dry_run_writes_no_entries_and_no_cursor(self):
        self.arl_catalog()
        source = self.source(mode=HarvestSource.Mode.CREATE)
        HarvestRecord.objects.create(source=source, record_id="old", status="unmatched", dry_run=True)
        snapshot = {e.pk: (e.updated_at, dict(e.identifiers or {})) for e in Entry.objects.all()}
        categories = Category.objects.count()

        report = run_source(source, dry_run=True)

        self.assertEqual({e.pk: (e.updated_at, dict(e.identifiers or {})) for e in Entry.objects.all()}, snapshot)
        self.assertEqual(Category.objects.count(), categories)
        source.refresh_from_db()
        self.assertIsNone(source.cursor)
        self.assertIsNone(source.last_run_at)
        self.assertTrue(source.last_report["dry_run"])
        self.assertEqual(report["created"], 1)
        rows = HarvestRecord.objects.filter(source=source)
        self.assertFalse(rows.filter(dry_run=False).exists())
        # The previous dry run's rows are gone; a deletion never seen live keeps its OAI identifier.
        self.assertEqual(
            set(rows.values_list("record_id", flat=True)),
            {"0000101", "0000102", "0000103", "oai:kis.example.org:STU_US_CAT*0000099"},
        )
        self.assertEqual(rows.get(record_id="0000103").status, "created")

    def test_full_harvest_drops_links_of_records_no_longer_listed(self):
        self.arl_catalog()
        gone = self.entry("Gone", identifiers={"arl_stu": "0000050"})
        source = self.source()
        HarvestRecord.objects.create(source=source, record_id="0000050", status="matched", entry=gone)

        report = run_source(source, full=True)

        gone.refresh_from_db()
        self.assertNotIn("arl_stu", gone.identifiers)
        self.assertEqual(
            HarvestRecord.objects.get(source=source, record_id="0000050", dry_run=False).status, "deleted"
        )
        self.assertEqual(report["deleted"], 2)

    def test_a_second_run_is_refused_while_one_holds_the_lock(self):
        source = self.source()
        with patch("apps.harvest.matching._lock_key", return_value=1):
            with patch("apps.harvest.matching.connection") as connection:
                connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False,)
                with self.assertRaises(SourceBusy):
                    run_source(source)


class ApplyTests(HarvestTestCase):
    def run_records(self, records, **source):
        source.setdefault("mode", HarvestSource.Mode.CREATE)
        self.harvest_source = self.source(**source)
        return run_source(self.harvest_source, adapter=ListAdapter(records))

    def test_equal_titles_with_different_isbns_are_both_created(self):
        report = self.run_records(
            [
                book("R1", title="Matematika", identifiers={"isbn": "9788022712347"}, edition="1. vyd."),
                book("R2", title="Matematika", identifiers={"isbn": "9788022750783"}, edition="2. vyd."),
            ]
        )
        self.assertEqual(report["created"], 2, report)
        self.assertEqual(
            sorted(Entry.objects.filter(title="Matematika").values_list("edition", flat=True)), ["1. vyd.", "2. vyd."]
        )

    def test_equal_titles_without_isbn_or_doi_are_refused(self):
        self.entry("Matematika")
        report = self.run_records([book("R1", title="Matematika", identifiers={})])
        self.assertEqual(report["failed"], 1)
        row = HarvestRecord.objects.get(record_id="R1")
        self.assertIn("Matematika", row.message)

    def test_record_without_language_fails_in_create_mode(self):
        report = self.run_records([book("R1", language=None)])
        self.assertEqual(report["failed"], 1)
        self.assertIn("language", HarvestRecord.objects.get(record_id="R1").message)

    def test_existing_category_term_is_reused_whatever_its_scheme(self):
        existing = Category.objects.create(catalog=self.catalog, term="004", scheme=None, creator=self.superuser)
        self.run_records(
            [book("R1", subjects=[SourceSubject("004", UDC_SCHEME), SourceSubject("Informatika", "arl-stu")])]
        )
        entry = Entry.objects.get(identifiers__arl_stu="R1")
        self.assertEqual(
            set(entry.categories.values_list("pk", flat=True)) - {existing.pk},
            set(Category.objects.filter(term="Informatika", scheme="arl-stu").values_list("pk", flat=True)),
        )
        self.assertIn(existing, entry.categories.all())
        self.assertEqual(Category.objects.filter(catalog=self.catalog, term="004").count(), 1)

    def test_authors_are_reused_per_catalog(self):
        self.run_records([book("R1"), book("R2", identifiers={"isbn": "9788022750783"}, title="Iná")])
        from apps.core.models import Author

        self.assertEqual(Author.objects.filter(catalog=self.catalog, surname="Macák").count(), 1)

    def test_link_only_works_on_an_entry_without_language(self):
        entry = self.entry("Legacy", language=None, identifiers={"isbn": "9788022712347"})
        report = self.run_records([book("R1")], mode=HarvestSource.Mode.LINK_ONLY)
        entry.refresh_from_db()
        self.assertEqual(entry.identifiers["arl_stu"], "R1")
        self.assertEqual(report["matched"], 1)

    def test_enrich_without_language_writes_the_columns_directly(self):
        entry = self.entry("Legacy", language=None, identifiers={"isbn": "9788022712347"})
        self.run_records([book("R1", publisher="Spektrum", language=None)], mode=HarvestSource.Mode.ENRICH)
        entry.refresh_from_db()
        self.assertEqual(entry.publisher, "Spektrum")

    def test_a_link_removed_by_hand_is_set_again(self):
        entry = self.entry("Kniha", identifiers={"isbn": "9788022712347"})
        self.run_records([book("R1")], mode=HarvestSource.Mode.LINK_ONLY)
        Entry.objects.filter(pk=entry.pk).update(identifiers={"isbn": "9788022712347"})

        report = run_source(self.harvest_source, adapter=ListAdapter([book("R1")]))

        entry.refresh_from_db()
        self.assertEqual(entry.identifiers.get("arl_stu"), "R1")
        self.assertEqual(report["unchanged"], 0)

    def test_unmatched_records_are_matched_again_when_the_entry_appears(self):
        self.run_records([book("R1")], mode=HarvestSource.Mode.LINK_ONLY)
        entry = self.entry("Kniha", identifiers={"isbn": "9788022712347"})

        report = run_source(self.harvest_source, adapter=ListAdapter([book("R1")]))

        self.assertEqual(report["matched"], 1)
        entry.refresh_from_db()
        self.assertEqual(entry.identifiers["arl_stu"], "R1")

    def test_unreadable_record_is_failed_and_the_run_goes_on(self):
        report = self.run_records([SourceRecord(record_id="X", extra={"error": "bad MARC"}), book("R1")])
        self.assertEqual((report["failed"], report["created"]), (1, 1))

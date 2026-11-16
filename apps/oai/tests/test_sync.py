"""IP-033 Phase 1: exposure, fingerprints and datestamps, tombstones, signals, reconcile, `oai_sync`."""

import io
import json
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.core.models import Acquisition, Author, Catalog, Category, Entry, EntryAuthor
from apps.oai import projection
from apps.oai.models import OaiRecord
from apps.oai.signals import dedupe_key
from apps.oai.sync import OaiRecordSync
from apps.oai.tests.base import LONG_AGO, PORTAL, UDC, OaiTestCase
from apps.readium.models import EncryptedContent


class ExposureTests(OaiTestCase):
    def exposed(self, entry) -> bool:
        entry = projection.load([entry.pk])[0]
        return projection.is_exposed(entry, projection.registered_entry_ids([entry.pk]))

    def test_an_entry_with_a_file_in_a_harvestable_catalog_is_exposed(self):
        self.assertTrue(self.exposed(self.entry()))

    def test_a_catalog_that_is_not_harvestable_hides_its_entries(self):
        self.assertFalse(self.exposed(self.entry(catalog=self.private)))

    def test_an_entry_without_a_file_is_held_back(self):
        self.assertFalse(self.exposed(self.entry(acquisition=False)))

    def test_entries_from_a_non_reexposed_source_and_withdrawn_entries_are_not_exposed(self):
        from apps.harvest.exposure import forget
        from apps.harvest.models import HarvestSource

        HarvestSource.objects.create(
            slug="dataverse",
            name="Repository",
            adapter="oai_pmh",
            base_url="https://x.example/oai",
            system_key="dataverse_pid",
            target_catalog=self.private,
            oai_reexpose=False,
        )
        forget()
        self.assertFalse(self.exposed(self.entry(origin="harvest:dataverse")))
        self.assertFalse(self.exposed(self.entry(withdrawn_at=timezone.now())))
        self.assertTrue(self.exposed(self.entry(origin="harvest:arl-stu")))

    def test_an_lcp_title_waits_for_a_registered_package(self):
        entry = self.entry(config={"readium_enabled": True})
        self.assertFalse(self.exposed(entry))
        encrypted = EncryptedContent.objects.create(
            acquisition=entry.acquisitions.get(),
            lcp_content_id=str(uuid.uuid4()),
            encrypted_path="x.lcpdf",
            status=EncryptedContent.EncryptionStatus.PENDING,
        )
        self.assertFalse(self.exposed(entry))
        encrypted.status = EncryptedContent.EncryptionStatus.REGISTERED
        encrypted.encrypted_at = timezone.now()
        encrypted.save()
        self.assertTrue(self.exposed(entry))

    def test_never_exposed_entries_get_no_row(self):
        entry = self.entry(catalog=self.private)
        result = self.sync(entry)
        self.assertEqual(result.skipped, 1)
        self.assertFalse(OaiRecord.objects.filter(entry_id=entry.pk).exists())


class ProjectionTests(OaiTestCase):
    def test_the_projection_resolves_everything_the_crosswalks_render(self):
        entry = self.entry(
            authors=(("Novák", "Ján"), ("Kováč", "Peter")),
            identifiers={"isbn": "80-227-1234-5", "doi": "doi:10.1234/ABC", "arl_stu": "0076487"},
            publisher="STU",
            published_at="2019-05",
            edition="2. vyd.",
            page_count=321,
            summary="<p>Učebnica\x0b strojov.</p>",
            rights={"license_url": "https://creativecommons.org/licenses/by/4.0/", "updated_at": "x"},
        )
        entry.categories.add(self.udc)
        data = projection.project(projection.load([entry.pk])[0])

        self.assertEqual(data["identifier"], f"oai:evilflowers.elvira.stuba.sk:{entry.pk}")
        self.assertEqual([a["surname"] for a in data["authors"]], ["Novák", "Kováč"])
        self.assertEqual(data["subjects"][0]["scheme_name"], "UDC")
        self.assertEqual(data["subjects"][0]["label"], "Elektrotechnika")
        self.assertEqual(data["identifiers"], {"isbn": "9788022712347", "doi": "10.1234/abc"})
        self.assertEqual(data["external_ids"][0]["record_id"], "0076487")
        self.assertEqual(
            data["external_ids"][0]["url"], "https://kis.cvt.stuba.sk/arl-stu/sk/detail-stu_us_cat-0076487/"
        )
        self.assertEqual(data["language"], {"alpha2": "sk", "alpha3": "slk"})
        self.assertEqual(data["published_at"], "2019-05")
        self.assertEqual(data["summary"], "Učebnica strojov.")
        self.assertEqual(data["rights"], {"license_url": "https://creativecommons.org/licenses/by/4.0/"})
        self.assertIsNone(data["access"])
        self.assertEqual(data["formats"], ["application/pdf"])
        self.assertEqual(data["url"], f"{PORTAL}/library?entry-detail-id={entry.pk}")
        self.assertEqual(projection.set_specs(entry), ["stu", f"stu:{self.udc.pk}"])

    def test_no_portal_url_while_cite_base_url_is_empty(self):
        entry = self.entry()
        with patch("apps.oai.projection.settings_store.get", return_value=""):
            data = projection.project(projection.load([entry.pk])[0])
        self.assertIsNone(data["url"])

    def test_an_lcp_title_carries_an_access_statement(self):
        entry = self.entry(config={"readium_enabled": True})
        data = projection.project(projection.load([entry.pk])[0])
        self.assertEqual(data["access"]["statement"], projection.LCP_ACCESS_STATEMENT)

    def test_the_fingerprint_covers_sets_and_the_crosswalk_version(self):
        base = projection.fingerprint({"title": "A"}, ["stu"])
        self.assertNotEqual(base, projection.fingerprint({"title": "A"}, ["stu", "stu:x"]))
        with patch("apps.oai.projection.CROSSWALK_VERSION", 99):
            self.assertNotEqual(base, projection.fingerprint({"title": "A"}, ["stu"]))


class DatestampTests(OaiTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry()
        result = self.sync(self.book)
        self.assertEqual(result.created, 1)
        self.backdate(self.book)

    def test_the_first_sync_stamps_whole_seconds(self):
        entry = self.entry(title="Iná kniha")
        self.sync(entry)
        self.assertEqual(self.record(entry).datestamp.microsecond, 0)

    def test_a_read_does_not_move_the_datestamp(self):
        # What opening a book does (files/views.py): popularity + 1 and a plain save.
        book = Entry.objects.get(pk=self.book.pk)
        book.popularity += 1
        book.save()
        result = self.sync(self.book)
        self.assertEqual(result.unchanged, 1)
        self.assertFalse(self.moved(self.book))

    def test_a_title_edit_moves_it(self):
        Entry.objects.filter(pk=self.book.pk).update(title="Elektrické stroje II")
        self.assertEqual(self.sync(self.book).updated, 1)
        self.assertTrue(self.moved(self.book))
        self.assertEqual(self.record(self.book).projection["title"], "Elektrické stroje II")

    def test_an_author_rename_moves_it(self):
        Author.objects.filter(entry_authors__entry=self.book).update(surname="Nováková")
        self.sync(self.book)
        self.assertTrue(self.moved(self.book))

    def test_a_category_added_moves_it_and_its_sets(self):
        self.book.categories.add(self.udc)
        self.sync(self.book)
        self.assertTrue(self.moved(self.book))
        self.assertIn(f"stu:{self.udc.pk}", self.record(self.book).set_specs)

    def test_a_file_added_moves_it(self):
        Acquisition.objects.create(entry=self.book, mime=Acquisition.AcquisitionMIME.EPUB)
        self.sync(self.book)
        self.assertTrue(self.moved(self.book))
        self.assertEqual(self.record(self.book).projection["formats"], ["application/epub+zip", "application/pdf"])

    def test_an_external_id_moves_it(self):
        Entry.objects.filter(pk=self.book.pk).update(identifiers={"arl_stu": "0076487"})
        self.sync(self.book)
        self.assertTrue(self.moved(self.book))

    def test_a_dry_run_counts_without_writing(self):
        Entry.objects.filter(pk=self.book.pk).update(title="Nový názov")
        result = self.sync(self.book, dry_run=True)
        self.assertEqual(result.updated, 1)
        self.assertFalse(self.moved(self.book))


class TombstoneTests(OaiTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry()
        self.book.categories.add(self.udc)
        self.sync(self.book)
        self.backdate(self.book)

    def assertTombstone(self, entry_id):
        record = self.record(entry_id)
        self.assertTrue(record.deleted)
        self.assertIsNone(record.projection)
        self.assertGreater(record.datestamp, LONG_AGO)
        # The last setSpecs stay: a selective harvest of the set learns of the deletion.
        self.assertEqual(record.set_specs, ["stu", f"stu:{self.udc.pk}"])

    def test_a_deleted_entry_leaves_a_tombstone(self):
        entry_id = self.book.pk
        self.book.delete()
        self.assertEqual(self.sync(entry_id).deleted, 1)
        self.assertTombstone(entry_id)

    def test_withdrawal_leaves_a_tombstone(self):
        Entry.objects.filter(pk=self.book.pk).update(withdrawn_at=timezone.now())
        self.sync(self.book)
        self.assertTombstone(self.book.pk)

    def test_catalog_opt_out_leaves_a_tombstone_that_is_not_stamped_again(self):
        Catalog.objects.filter(pk=self.stu.pk).update(harvestable=False)
        self.assertEqual(self.sync(self.book).deleted, 1)
        self.assertTombstone(self.book.pk)
        self.backdate(self.book)
        self.assertEqual(self.sync(self.book).unchanged, 1)
        self.assertFalse(self.moved(self.book))

    def test_re_exposure_revives_the_row(self):
        Catalog.objects.filter(pk=self.stu.pk).update(harvestable=False)
        self.sync(self.book)
        Catalog.objects.filter(pk=self.stu.pk).update(harvestable=True)
        self.backdate(self.book)
        self.assertEqual(self.sync(self.book).revived, 1)
        record = self.record(self.book)
        self.assertFalse(record.deleted)
        self.assertIsNotNone(record.projection)
        self.assertTrue(self.moved(self.book))
        self.assertEqual(OaiRecord.objects.filter(entry_id=self.book.pk).count(), 1)

    def orphan(self) -> OaiRecord:
        """A live row whose entry no longer exists (no `Entry.delete()` involved)."""
        return OaiRecord.objects.create(
            entry_id=uuid.uuid4(),
            set_specs=["stu"],
            datestamp=LONG_AGO,
            fingerprint="0" * 64,
            projection={"title": "Gone"},
        )

    def test_sync_and_reconcile_tombstone_a_row_without_entry(self):
        first, second = self.orphan(), self.orphan()
        self.assertEqual(self.sync(first.entry_id).deleted, 1)
        self.assertEqual(OaiRecordSync().reconcile().deleted, 1)
        for row in (first, second):
            row.refresh_from_db()
            self.assertTrue(row.deleted)
            self.assertIsNone(row.projection)
            self.assertGreater(row.datestamp, LONG_AGO)
            self.assertEqual(row.set_specs, ["stu"])

    def test_scheduling_a_row_without_entry_reaches_it(self):
        from apps.oai import signals

        row = self.orphan()
        with self.run_signals() as delay:
            signals.schedule([row.entry_id])
        delay.assert_called_once_with([str(row.entry_id)])
        row.refresh_from_db()
        self.assertTrue(row.deleted)

    def test_reconcile_tombstones_rows_whose_entry_is_gone(self):
        other = self.entry(title="Druhá")
        self.sync(other)
        Entry.objects.filter(pk=self.book.pk).delete()
        Entry.objects.filter(pk=other.pk).update(title="Druhá, opravená")
        result = OaiRecordSync().reconcile()
        self.assertEqual(result.deleted, 1)
        self.assertEqual(result.updated, 1)
        self.assertTombstone(self.book.pk)


class SignalTests(OaiTestCase):
    """Receivers enqueue on commit, deduplicated per entry (Q5); the task does the comparing."""

    def test_creating_an_entry_with_a_file_creates_its_record(self):
        with self.run_signals():
            book = self.entry()
        self.assertFalse(self.record(book).deleted)

    def test_edits_through_the_orm_reach_the_record(self):
        with self.run_signals():
            book = self.entry()
        self.backdate(book)

        with self.run_signals():
            book.title = "Nový názov"
            book.save()
        self.assertTrue(self.moved(book))

        self.backdate(book)
        with self.run_signals():
            book.categories.add(self.udc)
        self.assertTrue(self.moved(book))

        self.backdate(book)
        with self.run_signals():
            self.udc.label = {"sk": "Elektrotechnika a energetika"}
            self.udc.save()
        self.assertTrue(self.moved(book))

        self.backdate(book)
        with self.run_signals():
            author = book.authors.get()
            author.name = "Jozef"
            author.save()
        self.assertTrue(self.moved(book))

    def test_a_read_enqueues_but_does_not_move_the_datestamp(self):
        with self.run_signals():
            book = self.entry()
        self.backdate(book)
        with self.run_signals() as delay:
            book.popularity += 1
            book.save()
        self.assertEqual(delay.call_count, 1)
        self.assertFalse(self.moved(book))

    def test_a_save_of_unprojected_columns_enqueues_nothing(self):
        with self.run_signals():
            book = self.entry()
        with self.run_signals() as delay:
            book.popularity += 1
            book.save(update_fields=["popularity"])
        delay.assert_not_called()

    def test_entries_outside_harvestable_catalogs_cost_no_task(self):
        with self.run_signals() as delay:
            self.entry(catalog=self.private)
        delay.assert_not_called()

    def test_a_pending_sync_suppresses_another(self):
        with self.run_signals():
            book = self.entry()
        cache.add(dedupe_key(book.pk), 1, 60)
        self.addCleanup(cache.delete, dedupe_key(book.pk))
        with self.run_signals() as delay:
            book.title = "Iný"
            book.save()
        delay.assert_not_called()

    def test_deleting_an_entry_its_catalog_or_a_category_reaches_the_records(self):
        with self.run_signals():
            first = self.entry()
            second = self.entry(title="Druhá")
            first.categories.add(self.udc)
        first_id, second_id = first.pk, second.pk

        self.backdate(first)
        with self.run_signals():
            self.udc.delete()
        self.assertNotIn(f"stu:{self.udc.pk}", self.record(first_id).set_specs)
        self.assertTrue(self.moved(first_id))

        with self.run_signals():
            first.delete()
        self.assertTrue(self.record(first_id).deleted)

        with self.run_signals():
            self.stu.delete()
        self.assertTrue(self.record(second_id).deleted)

    def test_catalog_opt_out_and_opt_in(self):
        with self.run_signals():
            book = self.entry()
        with self.run_signals():
            self.stu.harvestable = False
            self.stu.save()
        self.assertTrue(self.record(book).deleted)
        with self.run_signals():
            self.stu.harvestable = True
            self.stu.save()
        self.assertFalse(self.record(book).deleted)

    def test_a_removed_file_and_a_registered_package_change_exposure(self):
        with self.run_signals():
            book = self.entry(config={"readium_enabled": True})
        self.assertFalse(OaiRecord.objects.filter(entry_id=book.pk).exists())
        with self.run_signals():
            EncryptedContent.objects.create(
                acquisition=book.acquisitions.get(),
                lcp_content_id=str(uuid.uuid4()),
                encrypted_path="x.lcpdf",
                status=EncryptedContent.EncryptionStatus.REGISTERED,
                encrypted_at=timezone.now(),
            )
        self.assertFalse(self.record(book).deleted)

        plain = None
        with self.run_signals():
            plain = self.entry(title="Bez súboru")
        with self.run_signals():
            plain.acquisitions.all().delete()
        self.assertTrue(self.record(plain).deleted)

    def test_a_rolled_back_change_enqueues_nothing(self):
        from django.db import transaction

        with self.run_signals() as delay:
            try:
                with transaction.atomic():
                    self.entry()
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
            self.entry(catalog=self.private)
        delay.assert_not_called()

    @override_settings(EVILFLOWERS_OAI_ENABLED=False)
    def test_nothing_is_enqueued_while_disabled(self):
        with self.run_signals() as delay:
            self.entry()
        delay.assert_not_called()

    def test_entry_author_changes_reach_the_record(self):
        with self.run_signals():
            book = self.entry()
        self.backdate(book)
        with self.run_signals():
            author = Author.objects.create(catalog=self.stu, surname="Druhý", name="Autor")
            EntryAuthor.objects.create(entry=book, author=author, position=5)
        self.assertEqual(len(self.record(book).projection["authors"]), 2)
        self.backdate(book)
        with self.run_signals():
            EntryAuthor.objects.filter(entry=book, author=author).delete()
        self.assertEqual(len(self.record(book).projection["authors"]), 1)
        self.assertTrue(self.moved(book))

    def test_reverse_category_changes_reach_the_record(self):
        with self.run_signals():
            book = self.entry()
            category = Category.objects.create(creator=self.owner, catalog=self.stu, term="004", scheme=UDC)
        with self.run_signals():
            category.entries.add(book)
        self.assertIn(f"stu:{category.pk}", self.record(book).set_specs)
        with self.run_signals():
            category.entries.clear()
        self.assertNotIn(f"stu:{category.pk}", self.record(book).set_specs)


class CommandTests(OaiTestCase):
    def test_oai_sync_all_and_entry_with_dry_run(self):
        book = self.entry()
        out = io.StringIO()
        call_command("oai_sync", "--all", "--dry-run", stdout=out)
        self.assertIn('"created": 1', out.getvalue())
        self.assertFalse(OaiRecord.objects.exists())

        out = io.StringIO()
        call_command("oai_sync", "--entry", str(book.pk), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["created"], 1)
        self.assertTrue(OaiRecord.objects.filter(entry_id=book.pk).exists())

    def test_reconcile_task_and_job_kind(self):
        from apps.oai.tasks import reconcile
        from apps.tasks.contract import registry

        book = self.entry()
        self.assertEqual(reconcile()["created"], 1)
        self.assertTrue(OaiRecord.objects.filter(entry_id=book.pk).exists())
        self.assertEqual(registry.for_task("apps.oai.tasks.reconcile").kind, "maintenance.oai_reconcile")

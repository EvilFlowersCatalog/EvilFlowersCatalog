"""`delete_entry` and the access settings of `update_entry` (housekeeping on elvira-dev, 2026-11-08).

The MCP could delete catalogs, feeds and categories but not a publication, so
removing a handful of test uploads needed the REST API. And `update_entry`
could protect a title with LCP but not set the IP block or the viewer switches,
which needed a REST `PUT` with a hand-written `config`.
"""

from datetime import timedelta

from django.db.models import RestrictedError
from django.utils import timezone

from apps.core.models import Acquisition, Entry
from apps.mcp.tests.test_management import ManagementTestCase
from apps.readium.models import EncryptedContent, License, Reservation


class DeleteEntryTests(ManagementTestCase):
    def setUp(self):
        super().setUp()
        self.doomed = Entry.objects.create(creator=self.manager, catalog=self.catalog, title="Test upload")

    def _loan(self, *, expires_in=timedelta(days=7), with_package=False) -> License:
        encrypted = None
        if with_package:
            acquisition = Acquisition.objects.create(entry=self.doomed, mime="application/pdf")
            encrypted = EncryptedContent.objects.create(
                acquisition=acquisition,
                lcp_content_id=f"content-{acquisition.pk}",
                encrypted_path=f"catalogs/managed/{self.doomed.pk}/encrypted/x.lcpdf",
                status=EncryptedContent.EncryptionStatus.REGISTERED,
            )
        now = timezone.now()
        return License.objects.create(
            entry=self.doomed,
            user=self.reader,
            encrypted_content=encrypted,
            state=License.LicenseState.ACTIVE,
            starts_at=now - timedelta(days=1),
            expires_at=now + expires_in,
        )

    def test_dry_run_reports_without_deleting(self):
        payload = self.ok("delete_entry", {"entry_id": str(self.doomed.pk), "dry_run": True}, self.manager)

        self.assertFalse(payload["deleted"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["folder"], f"catalogs/managed/{self.doomed.pk}")
        self.assertTrue(Entry.objects.filter(pk=self.doomed.pk).exists())

    def test_a_manager_can_delete_a_publication(self):
        payload = self.ok("delete_entry", {"entry_id": str(self.doomed.pk)}, self.manager)

        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["title"], "Test upload")
        self.assertFalse(Entry.objects.filter(pk=payload["id"]).exists())

    def test_a_reader_cannot_delete(self):
        self.refused("delete_entry", {"entry_id": str(self.doomed.pk)}, self.reader)
        self.assertTrue(Entry.objects.filter(pk=self.doomed.pk).exists())

    def test_an_active_loan_blocks_deletion_unless_forced(self):
        self._loan()

        message = self.refused("delete_entry", {"entry_id": str(self.doomed.pk)}, self.manager)
        self.assertIn("active loan", message)
        self.assertTrue(Entry.objects.filter(pk=self.doomed.pk).exists())

        self.assertTrue(
            self.ok("delete_entry", {"entry_id": str(self.doomed.pk), "force": True}, self.manager)["deleted"]
        )

    def test_a_queued_reservation_blocks_deletion(self):
        Reservation.objects.create(entry=self.doomed, user=self.reader, position=1)
        message = self.refused("delete_entry", {"entry_id": str(self.doomed.pk)}, self.manager)
        self.assertIn("reservation", message)

    def test_loan_history_on_an_encrypted_publication_does_not_block_deletion(self):
        """`License.encrypted_content` is PROTECT; an expired loan must not make the entry undeletable."""
        self._loan(expires_in=timedelta(days=-1), with_package=True)

        payload = self.ok("delete_entry", {"entry_id": str(self.doomed.pk)}, self.manager)

        self.assertTrue(payload["deleted"])
        self.assertEqual(len(payload["lcp_content_ids"]), 1)
        self.assertFalse(License.objects.filter(entry_id=payload["id"]).exists())

    def test_deleting_only_the_package_under_a_loan_is_still_refused(self):
        """What PROTECT was there for: an acquisition cannot vanish from under its licenses."""
        loan = self._loan(with_package=True)
        with self.assertRaises(RestrictedError):
            loan.encrypted_content.acquisition.delete()


class AccessSettingsTests(ManagementTestCase):
    def test_viewer_switches_and_ip_block_map_to_config(self):
        self.ok(
            "update_entry",
            {
                "entry_id": str(self.entry.pk),
                "ip_block": True,
                "allow_print": False,
                "allow_share": False,
                "allow_annotations": False,
            },
            self.manager,
        )

        self.entry.refresh_from_db()
        self.assertIs(self.entry.read_config("evilflowers_ip_block"), True)
        self.assertIs(self.entry.read_config("evilflowers_viewer_print"), False)
        self.assertIs(self.entry.read_config("evilflowers_share_enabled"), False)
        self.assertIs(self.entry.read_config("evilflowers_annotations_create"), False)

    def test_access_settings_alone_leave_the_metadata_alone(self):
        before = Entry.objects.get(pk=self.entry.pk)
        self.ok("update_entry", {"entry_id": str(self.entry.pk), "ip_block": True}, self.manager)
        after = Entry.objects.get(pk=self.entry.pk)

        self.assertEqual(
            (after.title, after.language_id, after.summary), (before.title, before.language_id, before.summary)
        )
        self.assertFalse(after.read_config("readium_enabled"))

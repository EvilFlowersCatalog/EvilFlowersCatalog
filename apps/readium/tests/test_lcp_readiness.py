"""Availability waits for the registered package.

Found on elvira-dev on 2026-11-08: titles with encryption `pending` (for four
months), `failed`, or not even started reported `available_now` with free
copies, so readers were offered a loan that could only fail.
"""

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.serializers.entries import LcpState
from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.readium.models import EncryptedContent
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class LcpReadinessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="reader", name="Re", surname="Ader", auth_source=auth_source)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Lending", url_name="lending")

    def setUp(self):
        self.entry = Entry.objects.create(
            creator=self.user,
            catalog=self.catalog,
            title="Protected",
            config={"readium_enabled": True, "readium_amount": 3},
        )
        # External acquisition: no local file, so saving it queues no encryption.
        self.acquisition = Acquisition.objects.create(entry=self.entry, mime="application/pdf")

    def state(self) -> dict:
        return lcp_state_mapping(self.user, [Entry.objects.get(pk=self.entry.pk)])[self.entry.pk]

    def _package(self, status, encrypted_at=None):
        return EncryptedContent.objects.create(
            acquisition=self.acquisition,
            lcp_content_id="content-r",
            encrypted_path="catalogs/lending/x/encrypted/content-r.lcpdf",
            status=status,
            encrypted_at=encrypted_at,
        )

    def test_no_package_yet_lends_nothing(self):
        row = self.state()
        self.assertFalse(row["lcp_ready"])
        self.assertEqual(row["available_slots"], 0)
        self.assertEqual(row["total_slots"], 3)
        self.assertEqual(row["lcp_state"], LcpState.FULLY_BORROWED)

    def test_pending_and_failed_jobs_lend_nothing(self):
        for status in (EncryptedContent.EncryptionStatus.PENDING, EncryptedContent.EncryptionStatus.FAILED):
            with self.subTest(status=status):
                EncryptedContent.objects.filter(acquisition=self.acquisition).delete()
                self._package(status)
                self.assertNotEqual(self.state()["lcp_state"], LcpState.AVAILABLE_NOW)

    def test_a_registered_package_is_available(self):
        self._package(EncryptedContent.EncryptionStatus.REGISTERED, encrypted_at=timezone.now())
        row = self.state()
        self.assertTrue(row["lcp_ready"])
        self.assertEqual(row["available_slots"], 3)
        self.assertEqual(row["lcp_state"], LcpState.AVAILABLE_NOW)

    def test_an_open_publication_is_unaffected(self):
        self.entry.config = {"readium_enabled": False}
        self.entry.save()
        self.assertEqual(self.state()["lcp_state"], LcpState.NOT_LCP)

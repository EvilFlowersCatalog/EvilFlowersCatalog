"""Switching LCP on (re)queues encryption — once, not on every save.

Found on elvira-dev on 2026-11-08: a job that `failed` (source PDF missing) was
never retried by turning protection off and on again, because the receiver
skipped any acquisition that already had an `EncryptedContent` row. The retry
must still not fire on ordinary saves, or a permanently failing job would be
resent on every metadata edit.
"""

import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.readium.models import EncryptedContent

SERVICE = "apps.readium.services.ContentEncryptionService"


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class EncryptionToggleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="editor", name="Ed", surname="Itor", auth_source=auth_source)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Toggle", url_name="toggle")

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        patcher = patch.object(Acquisition._meta.get_field("content"), "storage", FileSystemStorage(location=root))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.entry = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Toggled")
        self.acquisition = Acquisition(entry=self.entry, mime="application/pdf")
        self.acquisition.content.save("book.pdf", ContentFile(b"%PDF-1.7"), save=False)
        self.acquisition.save()

    def _set_lcp(self, enabled: bool):
        self.entry.refresh_from_db()
        self.entry.config = {**(self.entry.config or {}), "readium_enabled": enabled, "readium_amount": 1}
        self.entry.save()

    def _failed_job(self) -> EncryptedContent:
        return EncryptedContent.objects.create(
            acquisition=self.acquisition,
            lcp_content_id="failed-1",
            encrypted_path=f"catalogs/toggle/{self.entry.pk}/encrypted/failed-1.lcpdf",
            status=EncryptedContent.EncryptionStatus.FAILED,
        )

    def test_switching_on_queues_an_acquisition_without_a_job(self):
        with patch(SERVICE) as service:
            self._set_lcp(True)
        service.encrypt_acquisition.assert_called_once()
        service.requeue.assert_not_called()

    def test_switching_on_requeues_a_failed_job(self):
        self._failed_job()
        with patch(SERVICE) as service:
            self._set_lcp(True)
        service.requeue.assert_called_once()
        self.assertEqual(service.requeue.call_args.args[0].lcp_content_id, "failed-1")
        service.encrypt_acquisition.assert_not_called()

    def test_saving_an_already_protected_entry_does_not_requeue(self):
        self._failed_job()
        with patch(SERVICE):
            self._set_lcp(True)

        with patch(SERVICE) as service:
            self.entry.refresh_from_db()
            self.entry.title = "Toggled, renamed"
            self.entry.save()
        service.requeue.assert_not_called()
        service.encrypt_acquisition.assert_not_called()

    def test_a_registered_job_is_never_requeued(self):
        job = self._failed_job()
        job.status = EncryptedContent.EncryptionStatus.REGISTERED
        job.save()
        with patch(SERVICE) as service:
            self._set_lcp(True)
        service.requeue.assert_not_called()

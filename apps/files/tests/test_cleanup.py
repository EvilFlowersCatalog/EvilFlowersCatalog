"""Deleting rows removes the files they own (`apps.files.signals`).

Found on elvira-dev on 2026-11-08: deleting an entry left its PDF, cover,
thumbnail, extraction crops and LCP package in storage, although the OpenAPI
description of `DELETE .../entries/{id}` promised the opposite.

`TestCase` wraps each test in a transaction that never commits, so on-commit
callbacks only run inside `captureOnCommitCallbacks(execute=True)` — without it
these tests would pass because nothing ran at all.
"""

import os
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.test import TestCase, override_settings

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.readium.models import EncryptedContent


class CleanupTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="librarian", name="Libra", surname="Rian", auth_source=auth_source)
        cls.user.set_unusable_password()
        cls.user.save()

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(
            EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=self.root, EVILFLOWERS_EVENT_BROKER_EXECUTOR=None
        )
        override.enable()
        self.addCleanup(override.disable)
        # `get_storage()` reads the setting on every call, but a model field
        # evaluates its `storage=get_storage` callable once, at import — so the
        # fields have to be pointed at the temporary root explicitly, or the
        # test writes into the developer's real storage directory.
        temporary = FileSystemStorage(location=self.root)
        for model, field in ((Entry, "image"), (Entry, "thumbnail"), (Acquisition, "content")):
            patcher = patch.object(model._meta.get_field(field), "storage", temporary)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.catalog = Catalog.objects.create(creator=self.user, title="Cleanup", url_name="cleanup")
        self.entry = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Doomed Book")
        self.entry.image.save("cover.jpg", ContentFile(b"jpeg"), save=False)
        self.entry.thumbnail.save("thumbnail.jpg", ContentFile(b"jpeg"), save=False)
        self.entry.save()

        self.acquisition = Acquisition(entry=self.entry, mime="application/pdf")
        self.acquisition.content.save("book.pdf", ContentFile(b"%PDF-1.7"), save=False)
        self.acquisition.save()

        self.entry_dir = f"catalogs/cleanup/{self.entry.pk}"
        self.crop = self._write(f"{self.entry_dir}/extraction/{self.acquisition.pk}/run/figure-1.png")
        self.package = self._write(f"{self.entry_dir}/encrypted/content-1.lcpdf")
        self.source = self._write(f"{self.entry_dir}/encrypted/source/content-1/book.pdf")
        self.leftover = self._write(f"{self.entry_dir}/cover_OLD.jpg")
        EncryptedContent.objects.create(
            acquisition=self.acquisition,
            lcp_content_id="content-1",
            encrypted_path=f"{self.entry_dir}/encrypted/content-1.lcpdf",
        )

    def _write(self, relative: str) -> str:
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return relative

    def exists(self, relative: str) -> bool:
        return os.path.exists(os.path.join(self.root, relative))


class EntryDeletionTests(CleanupTestCase):
    def test_deleting_an_entry_removes_every_file_it_owned(self):
        owned = [
            self.entry.image.name,
            self.entry.thumbnail.name,
            self.acquisition.content.name,
            self.crop,
            self.package,
            self.source,
            self.leftover,
        ]
        self.assertTrue(all(self.exists(name) for name in owned))

        with self.captureOnCommitCallbacks(execute=True):
            self.entry.delete()

        self.assertEqual([name for name in owned if self.exists(name)], [])
        self.assertFalse(self.exists(self.entry_dir))

    def test_a_rolled_back_delete_keeps_the_files(self):
        entry_id = self.entry.pk  # the collector sets `pk = None` on the instance it deleted
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.entry.delete()
                    raise RuntimeError("abort")
            except RuntimeError:
                pass

        self.assertEqual(callbacks, [])
        self.assertTrue(self.exists(self.acquisition.content.name))
        self.assertTrue(Entry.objects.filter(pk=entry_id).exists())

    def test_deleting_a_catalog_cascades_to_the_files(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.catalog.delete()

        self.assertFalse(self.exists("catalogs/cleanup"))


class AcquisitionDeletionTests(CleanupTestCase):
    def test_deleting_an_acquisition_removes_its_file_crops_and_package_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.acquisition.delete()

        self.assertFalse(self.exists(self.acquisition.content.name))
        self.assertFalse(self.exists(self.crop))
        self.assertFalse(self.exists(self.package))
        self.assertFalse(self.exists(self.source))
        # The entry is still there, and so is its cover.
        self.assertTrue(self.exists(self.entry.image.name))
        self.assertTrue(self.exists(self.leftover))

    def test_an_external_url_acquisition_touches_nothing(self):
        external = Acquisition.objects.create(
            entry=self.entry,
            mime="application/pdf",
            file_url="https://dataverse.example.org/api/access/datafile/1",
            storage_backend=Acquisition.StorageBackend.EXTERNAL_URL,
        )

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            external.delete()

        self.assertEqual(callbacks, [])
        self.assertTrue(self.exists(self.entry.image.name))

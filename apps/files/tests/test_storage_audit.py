"""`manage.py storage_audit`: orphans (files without rows) and missing files (rows without files)."""

import json
import os
import shutil
import tempfile
import time
import uuid
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.core.models import Acquisition, AuthSource, Catalog, Entry, User
from apps.tasks.tasks import cleanup_orphans


class StorageAuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        auth_source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="auditor", name="Au", surname="Ditor", auth_source=auth_source)
        cls.user.set_unusable_password()
        cls.user.save()
        cls.catalog = Catalog.objects.create(creator=cls.user, title="Audited", url_name="audited")

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(
            EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=self.root, EVILFLOWERS_EVENT_BROKER_EXECUTOR=None
        )
        override.enable()
        self.addCleanup(override.disable)

        # A live entry whose book is on disk, and one whose book is not.
        self.present = Entry.objects.create(creator=self.user, catalog=self.catalog, title="On disk")
        self.present_book = f"catalogs/audited/{self.present.pk}/book.pdf"
        self._write(self.present_book)
        Acquisition.objects.create(entry=self.present, mime="application/pdf", content=self.present_book)

        self.lost = Entry.objects.create(creator=self.user, catalog=self.catalog, title="Lost in the move")
        self.lost_book = f"catalogs/audited/{self.lost.pk}/book.pdf"
        Acquisition.objects.create(entry=self.lost, mime="application/pdf", content=self.lost_book)

        # Leftovers of deletions that happened before the cleanup receivers existed.
        self.orphan_entry = f"catalogs/audited/{uuid.uuid4()}"
        self._write(f"{self.orphan_entry}/book.pdf")
        self.orphan_catalog = "catalogs/fchpt-old"
        self._write(f"{self.orphan_catalog}/{uuid.uuid4()}/thumbnail.jpg")

    def _write(self, relative: str):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x")

    def audit(self, *args) -> dict:
        out = StringIO()
        call_command("storage_audit", "--format", "json", *args, stdout=out)
        return json.loads(out.getvalue())

    def test_reports_orphans_and_missing_files_without_deleting(self):
        report = self.audit()

        self.assertEqual(
            {(item["kind"], item["path"]) for item in report["orphans"]},
            {("entry", self.orphan_entry), ("catalog", self.orphan_catalog)},
        )
        self.assertEqual(
            [(item["kind"], item["path"]) for item in report["missing"]], [("acquisition", self.lost_book)]
        )
        self.assertTrue(os.path.exists(os.path.join(self.root, self.orphan_entry)))

    def test_delete_orphans_removes_only_the_orphans(self):
        self.audit("--delete-orphans")

        self.assertFalse(os.path.exists(os.path.join(self.root, self.orphan_entry)))
        self.assertFalse(os.path.exists(os.path.join(self.root, self.orphan_catalog)))
        self.assertTrue(os.path.exists(os.path.join(self.root, self.present_book)))

    def test_min_age_keeps_recent_orphans(self):
        old = time.time() - 30 * 24 * 3600
        for root, _dirs, files in os.walk(os.path.join(self.root, self.orphan_catalog)):
            for name in files:
                os.utime(os.path.join(root, name), (old, old))

        report = self.audit("--delete-orphans", "--min-age-hours", "168")

        by_path = {item["path"]: item for item in report["orphans"]}
        self.assertTrue(by_path[self.orphan_entry].get("recent"))
        self.assertTrue(os.path.exists(os.path.join(self.root, self.orphan_entry)))
        self.assertTrue(by_path[self.orphan_catalog].get("deleted"))
        self.assertFalse(os.path.exists(os.path.join(self.root, self.orphan_catalog)))

    def test_cleanup_task(self):
        old = time.time() - 30 * 24 * 3600
        os.utime(os.path.join(self.root, self.orphan_entry, "book.pdf"), (old, old))

        with override_settings(EVILFLOWERS_ORPHAN_CLEANUP_MIN_AGE_HOURS=168):
            result = cleanup_orphans()

        # The entry orphan is old; the catalog orphan was written just now.
        self.assertEqual(result, {"deleted": 1, "kept_recent": 1})
        self.assertFalse(os.path.exists(os.path.join(self.root, self.orphan_entry)))
        self.assertTrue(os.path.exists(os.path.join(self.root, self.orphan_catalog)))
        self.assertTrue(os.path.exists(os.path.join(self.root, self.present_book)))

    def test_catalog_filter(self):
        report = self.audit("--catalog", "audited")
        self.assertEqual([item["path"] for item in report["orphans"]], [self.orphan_entry])

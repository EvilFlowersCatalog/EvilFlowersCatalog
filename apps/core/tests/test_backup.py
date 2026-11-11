"""`manage.py backup` behind PgBouncer (#103), and `dump_catalog` for the nightly run."""

import os
import shutil
import subprocess
import tarfile
import tempfile
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.management.commands.backup import pg_dump_env
from apps.core.management.compression import XZCompressionStrategy
from apps.core.models import AuthSource, Catalog, User


class PgDumpEnvTests(SimpleTestCase):
    @override_settings(EVILFLOWERS_BACKUP_PGHOST=None, EVILFLOWERS_BACKUP_PGPORT=None)
    def test_inherits_pghost_when_unset(self):
        with mock.patch.dict("os.environ", {"PGHOST": "pooler", "PGPORT": "6432"}):
            env = pg_dump_env()
        self.assertEqual((env["PGHOST"], env["PGPORT"]), ("pooler", "6432"))

    @override_settings(EVILFLOWERS_BACKUP_PGHOST="172.28.1.1", EVILFLOWERS_BACKUP_PGPORT="5432")
    def test_direct_host_overrides_pooler(self):
        with mock.patch.dict("os.environ", {"PGHOST": "pooler", "PGPORT": "6432"}):
            env = pg_dump_env()
        self.assertEqual((env["PGHOST"], env["PGPORT"]), ("172.28.1.1", "5432"))


class DumpCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        owner = User(username="owner", name="O", surname="W", auth_source=source)
        owner.set_unusable_password()
        owner.save()
        cls.catalog = Catalog.objects.create(creator=owner, title="Empty", url_name="empty")

    def test_a_catalog_without_stored_files_is_dumped(self):
        """No storage directory (no file uploaded yet) must not fail the nightly backup."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        output = os.path.join(root, "empty.tar.xz")
        with override_settings(
            EVILFLOWERS_STORAGE_DRIVER="apps.files.storage.filesystem.FileSystemStorage",
            EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=os.path.join(root, "storage"),
        ):
            call_command("dump_catalog", id=str(self.catalog.pk), output=output, compression="xz", verbosity=0)
        with tarfile.open(output) as tar:
            self.assertEqual(tar.getnames(), ["entities.json"])

    def test_xz_uses_a_fast_preset(self):
        with mock.patch("apps.core.management.compression.tarfile.open") as open_:
            XZCompressionStrategy().open_tarfile("/tmp/x.tar.xz")
        open_.assert_called_once_with("/tmp/x.tar.xz", "w:xz", preset=1)


class BackupFailureTests(SimpleTestCase):
    def test_failed_pg_dump_raises(self):
        """The Celery task must fail, not report success, when pg_dump fails."""
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="connection refused")
        with (
            mock.patch("apps.core.management.commands.backup.subprocess.run", return_value=failed),
            mock.patch("apps.core.management.commands.backup.BackupDestination"),
        ):
            with self.assertRaisesMessage(CommandError, "connection refused"):
                call_command("backup", destination="/nonexistent")

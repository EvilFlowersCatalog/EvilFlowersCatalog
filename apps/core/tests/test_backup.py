"""`manage.py backup` behind PgBouncer (#103)."""

import subprocess
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from apps.core.management.commands.backup import pg_dump_env


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

"""Loading AGE as a non-superuser and surviving a refused graph (#147). No AGE needed: the cursor is mocked."""

import importlib
from unittest import mock

import psycopg
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from apps.graph import age

migration = importlib.import_module("apps.graph.migrations.0001_age_library_graph")


class _Cursor:
    """Records statements; raises the error mapped to a statement the first time it runs."""

    def __init__(self, errors=None):
        self.statements = []
        self.errors = dict(errors or {})

    def execute(self, sql, params=None):
        self.statements.append(sql)
        error = self.errors.pop(sql, None)
        if error is not None:
            raise error


class PrepareTests(SimpleTestCase):
    def setUp(self):
        age._load_statement = None
        self.addCleanup(setattr, age, "_load_statement", None)

    def test_plugins_path_first_and_remembered(self):
        cursor = _Cursor()
        age._prepare(cursor)
        self.assertEqual(
            cursor.statements[:3],
            ["SAVEPOINT evilflowers_age_load", age.PLUGIN_LOAD, "RELEASE SAVEPOINT evilflowers_age_load"],
        )
        self.assertTrue(cursor.statements[3].startswith("SET LOCAL search_path"))
        again = _Cursor()
        age._prepare(again)
        self.assertEqual(again.statements[0], age.PLUGIN_LOAD)
        self.assertEqual(len(again.statements), 2)

    def test_falls_back_to_bare_load_without_the_plugins_link(self):
        cursor = _Cursor(
            {age.PLUGIN_LOAD: psycopg.errors.UndefinedFile('could not access file "$libdir/plugins/age"')}
        )
        age._prepare(cursor)
        self.assertEqual(
            cursor.statements[:5],
            [
                "SAVEPOINT evilflowers_age_load",
                age.PLUGIN_LOAD,
                "ROLLBACK TO SAVEPOINT evilflowers_age_load",
                "RELEASE SAVEPOINT evilflowers_age_load",
                age.BARE_LOAD,
            ],
        )
        again = _Cursor()
        age._prepare(again)
        self.assertEqual(again.statements[0], age.BARE_LOAD)
        self.assertNotIn(age.PLUGIN_LOAD, again.statements)

    def test_falls_back_on_insufficient_privilege(self):
        cursor = _Cursor({age.PLUGIN_LOAD: psycopg.errors.InsufficientPrivilege("permission denied")})
        age._prepare(cursor)
        self.assertIn(age.BARE_LOAD, cursor.statements)

    def test_both_refused_raises_and_remembers_nothing(self):
        cursor = _Cursor(
            {
                age.PLUGIN_LOAD: psycopg.errors.UndefinedFile("no plugins link"),
                age.BARE_LOAD: psycopg.errors.InsufficientPrivilege('access to library "age" is not allowed'),
            }
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            age._prepare(cursor)
        self.assertIsNone(age._load_statement)

    def test_other_errors_are_not_swallowed(self):
        cursor = _Cursor({age.PLUGIN_LOAD: psycopg.errors.QueryCanceled("canceled")})
        with self.assertRaises(psycopg.errors.QueryCanceled):
            age._prepare(cursor)
        self.assertNotIn(age.BARE_LOAD, cursor.statements)


class PermissionHintTests(SimpleTestCase):
    def test_hints(self):
        self.assertIn("plugins", age.permission_hint(psycopg.errors.UndefinedFile("x")))
        self.assertIn(
            "plugins",
            age.permission_hint(psycopg.errors.InsufficientPrivilege('access to library "age" is not allowed')),
        )
        self.assertIn(
            "USAGE on schema ag_catalog",
            age.permission_hint(psycopg.errors.InsufficientPrivilege("permission denied for schema ag_catalog")),
        )
        self.assertIsNone(age.permission_hint(ValueError("x")))


class _SchemaEditor:
    def __init__(self):
        self.connection = mock.MagicMock()


class MigrationTests(SimpleTestCase):
    def _enable(self, create_graph):
        editor = _SchemaEditor()
        with (
            mock.patch.object(age, "extension_installable", return_value=True),
            mock.patch.object(migration.transaction, "atomic"),
            mock.patch.object(age, "create_graph", create_graph),
        ):
            migration.enable(None, editor)
        return editor

    def test_enable_passes_when_create_graph_is_refused(self):
        refused = mock.Mock(side_effect=psycopg.errors.InsufficientPrivilege('access to library "age" is not allowed'))
        with self.assertLogs(migration.logger, "WARNING") as logs:
            self._enable(refused)
        refused.assert_called_once()
        self.assertIn("off until `manage.py graph_rebuild`", logs.output[0])
        self.assertIn("plugins", logs.output[0])

    def test_enable_creates_the_graph(self):
        created = mock.Mock(return_value=True)
        self._enable(created)
        created.assert_called_once()


class GraphRebuildTests(SimpleTestCase):
    def test_permission_error_names_the_missing_piece(self):
        refused = psycopg.errors.InsufficientPrivilege("permission denied for database evilflowers")
        with mock.patch.object(age, "create_graph", side_effect=refused):
            with self.assertRaisesMessage(CommandError, "CREATE on the database"):
                call_command("graph_rebuild")

    def test_missing_extension(self):
        with mock.patch.object(age, "create_graph", return_value=False):
            with self.assertRaisesMessage(CommandError, "not installed"):
                call_command("graph_rebuild")

    def test_other_errors_propagate(self):
        with mock.patch.object(age, "create_graph", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                call_command("graph_rebuild")

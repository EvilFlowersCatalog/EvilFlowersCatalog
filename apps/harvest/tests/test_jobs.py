"""The `harvest.run` job kind, the beat tick, the MCP tools and the commands (IP-034)."""

import io
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import RequestFactory, override_settings

from apps.core.models import Entry
from apps.harvest.jobs import HarvestRunDefinition, HarvestRunSpec
from apps.harvest.models import HarvestRecord
from apps.harvest.schedule import is_due
from apps.harvest.tasks import tick
from apps.harvest.tests.base import ARL_PAGES, FakeSession, HarvestTestCase
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
from apps.mcp.server import McpServer
from apps.mcp.tools import registry as tools
from apps.tasks.domain.values import Ready, Refused

UTC = timezone.utc


class DefinitionTests(HarvestTestCase):
    def test_check(self):
        definition = HarvestRunDefinition()
        self.assertIsInstance(definition.check(self.source(), HarvestRunSpec()), Ready)
        disabled = self.source(slug="off", enabled=False)
        self.assertIsInstance(definition.check(disabled, HarvestRunSpec()), Refused)
        self.assertIsInstance(definition.check(disabled, HarvestRunSpec(dry_run=True)), Ready)
        no_file = self.source(slug="file", adapter="marc_file", base_url=None)
        self.assertIsInstance(definition.check(no_file, HarvestRunSpec()), Refused)
        unknown = self.source(slug="unknown", adapter="gone")
        self.assertIsInstance(definition.check(unknown, HarvestRunSpec(dry_run=True)), Refused)

    def test_scope_and_call_arguments(self):
        source = self.source()
        definition = HarvestRunDefinition()
        self.assertEqual(definition.scope(source).catalog_id, self.catalog.pk)
        self.assertEqual(definition.subject_from_call([], {"source_id": str(source.pk)}), str(source.pk))
        dispatch = SimpleNamespace(subject=source, spec=HarvestRunSpec(dry_run=True))
        self.assertEqual(definition.kwargs(dispatch), {"source_id": str(source.pk), "dry_run": True, "full": False})


class TickTests(HarvestTestCase):
    def test_due_once_per_firing(self):
        source = self.source(schedule="0 3 * * *")
        source.last_run_at = datetime(2026, 11, 15, 3, 0, 30, tzinfo=UTC)
        self.assertFalse(is_due(source, now=datetime(2026, 11, 16, 2, 59, tzinfo=UTC)))
        self.assertTrue(is_due(source, now=datetime(2026, 11, 16, 3, 1, tzinfo=UTC)))
        source.last_run_at = datetime(2026, 11, 16, 3, 1, tzinfo=UTC)
        self.assertFalse(is_due(source, now=datetime(2026, 11, 16, 3, 6, tzinfo=UTC)))

    def test_never_due_without_schedule(self):
        self.assertFalse(is_due(self.source(schedule=""), now=datetime.now(UTC)))

    def test_tick_declares_due_enabled_sources_only(self):
        old = datetime.now(UTC) - timedelta(days=3)
        due = self.source(slug="due", schedule="* * * * *", last_run_at=old)
        self.source(slug="disabled", schedule="* * * * *", enabled=False, last_run_at=old)
        self.source(slug="manual", schedule="", last_run_at=old)
        with patch("apps.tasks.contract.declare") as declare:
            result = tick()
        declare.assert_called_once_with("harvest.run", due.pk, spec={"dry_run": False, "full": False})
        self.assertEqual(result["declared"], ["due"])


class McpToolTests(HarvestTestCase):
    def call(self, name, arguments, user):
        request = RequestFactory().post("/mcp/v1")
        request.user = user
        response = McpServer(tools).handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            LATEST_PROTOCOL_VERSION,
        )
        return response["result"]

    def test_list_and_get_with_counts(self):
        source = self.source()
        self.source(slug="other", target_catalog=self.other_catalog)
        HarvestRecord.objects.create(source=source, record_id="1", status="matched")
        HarvestRecord.objects.create(source=source, record_id="2", status="ambiguous")

        listed = self.call("list_harvest_sources", {}, self.manager)["structuredContent"]
        self.assertEqual([item["slug"] for item in listed["items"]], ["arl-stu"])
        detail = self.call("get_harvest_source", {"source_id": str(source.pk)}, self.manager)["structuredContent"]
        self.assertEqual(detail["source"]["record_counts"], {"ambiguous": 1, "matched": 1})

        records = self.call(
            "list_harvest_records", {"source_id": str(source.pk), "status": ["ambiguous"]}, self.manager
        )["structuredContent"]
        self.assertEqual([item["record_id"] for item in records["items"]], ["2"])

    def test_run_harvest_needs_manage_and_declares_a_dry_run_by_default(self):
        source = self.source()
        refused = self.call("run_harvest", {"source_id": str(source.pk)}, self.outsider)
        self.assertTrue(refused.get("isError"))

        job = MagicMock(pk="00000000-0000-0000-0000-000000000001", state="queued", reason=None)
        with patch("apps.mcp.tools.harvest.declare", return_value=job) as declare:
            result = self.call("run_harvest", {"source_id": str(source.pk)}, self.manager)
        self.assertFalse(result.get("isError"), result)
        declare.assert_called_once_with("harvest.run", source.pk, spec={"dry_run": True, "full": False})


class CommandTests(HarvestTestCase):
    def test_harvest_command_runs_in_process(self):
        entry = self.entry("Numerická matematika", identifiers={"isbn": "978-80-227-1234-7"})
        self.source()
        out = io.StringIO()
        with patch("apps.harvest.adapters.oai.requests.Session", return_value=FakeSession(ARL_PAGES)):
            call_command("harvest", "arl-stu", stdout=out)
        self.assertEqual(json.loads(out.getvalue())["matched"], 1)
        entry.refresh_from_db()
        self.assertEqual(entry.identifiers["arl_stu"], "0000101")

    def test_attach_files_by_isbn(self):
        entry = self.entry("Kniha", identifiers={"isbn": "80-227-1234-5"})
        self.entry("Iná", identifiers={"isbn": "9788022750783"})
        self.source()
        directory = Path(tempfile.mkdtemp())
        storage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.addCleanup(shutil.rmtree, storage, ignore_errors=True)
        (directory / "978-80-227-1234-7.pdf").write_bytes(b"%PDF-1.4 test")
        (directory / "9788022766661.pdf").write_bytes(b"%PDF-1.4 test")
        (directory / "notes.txt").write_text("ignored")

        out = io.StringIO()
        call_command(
            "harvest_attach_files", source="arl-stu", dir=str(directory), match="isbn", dry_run=True, stdout=out
        )
        report = json.loads(out.getvalue())
        self.assertEqual(len(report["would_attach"]), 1)
        self.assertEqual(report["unmatched"], ["9788022766661.pdf"])
        self.assertFalse(entry.acquisitions.exists())

        out = io.StringIO()
        with (
            override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=storage),
            patch("apps.extraction.services.queue"),
            patch("apps.api.services.entry.store_table_of_contents"),
            patch("apps.api.services.entry.queue_ocr"),
        ):
            call_command("harvest_attach_files", source="arl-stu", dir=str(directory), match="isbn", stdout=out)
        self.assertEqual(len(json.loads(out.getvalue())["attached"]), 1)
        self.assertEqual(Entry.objects.get(pk=entry.pk).acquisitions.count(), 1)

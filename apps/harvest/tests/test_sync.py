"""Generic two-way sync (IP-034 Part B) against an in-memory writing connector."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, Entry
from apps.harvest.adapters import registry
from apps.harvest.exposure import not_reexposed_origins
from apps.harvest.merge import LOCAL, REMOTE, merge
from apps.harvest.models import HarvestConflict, HarvestRecord, HarvestSource
from apps.harvest.records import SourceAuthor, SourceRecord
from apps.harvest.runner import run_source
from apps.harvest.signals import syncing
from apps.harvest.sync import apply_record, push
from apps.harvest.tests.base import SYSTEMS, HarvestTestCase
from apps.harvest.tests.fake import FakeConnector, FakeRemote, ReadOnlyConnector

SYNC_SYSTEMS = SYSTEMS + [{"key": "fake_id", "name": "Fake"}, {"key": "fake_internal", "name": "Fake internal"}]
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "harvest-sync"}}
UTC = timezone.utc
EARLY = datetime(2020, 1, 1, tzinfo=UTC)
LATE = datetime(2030, 1, 1, tzinfo=UTC)


class MergeTests(SimpleTestCase):
    def test_unchanged_on_both_sides_is_nothing(self):
        result = merge({"title": "A"}, {"title": "A"}, {"title": "A"}, EARLY, LATE)
        self.assertFalse(result.changed)
        self.assertEqual(result.merged, {"title": "A"})

    def test_one_side_changed_goes_to_the_other(self):
        result = merge(
            {"title": "A", "publisher": "P"},
            {"title": "B", "publisher": "P"},
            {"title": "A", "publisher": "Q"},
            LATE,
            EARLY,
        )
        self.assertEqual(result.to_remote, {"title": "B"})
        self.assertEqual(result.to_local, {"publisher": "Q"})
        self.assertEqual(result.conflicts, [])

    def test_both_changed_the_later_wins_and_is_a_conflict(self):
        result = merge({"title": "A"}, {"title": "Local"}, {"title": "Remote"}, EARLY, LATE)
        self.assertEqual(result.to_local, {"title": "Remote"})
        self.assertEqual([(c.field, c.winner) for c in result.conflicts], [("title", REMOTE)])
        result = merge({"title": "A"}, {"title": "Local"}, {"title": "Remote"}, LATE, EARLY)
        self.assertEqual(result.to_remote, {"title": "Local"})
        self.assertEqual(result.conflicts[0].winner, LOCAL)

    def test_both_changed_to_the_same_value_is_no_conflict(self):
        result = merge({"title": "A"}, {"title": "B"}, {"title": "B"}, EARLY, LATE)
        self.assertFalse(result.changed)
        self.assertEqual(result.conflicts, [])

    def test_no_base_never_clears_data(self):
        result = merge(None, {"summary": "Local", "publisher": None}, {"summary": None, "publisher": "P"}, LATE, EARLY)
        self.assertEqual(result.to_remote, {"summary": "Local"})
        self.assertEqual(result.to_local, {"publisher": "P"})
        self.assertEqual(result.conflicts, [])

    def test_no_base_two_values_is_a_conflict(self):
        result = merge(None, {"title": "L"}, {"title": "R"}, EARLY, LATE)
        self.assertEqual(result.to_local, {"title": "R"})
        self.assertEqual(len(result.conflicts), 1)

    def test_a_remote_clearing_a_field_wins_when_only_it_changed(self):
        result = merge({"summary": "S"}, {"summary": "S"}, {"summary": None}, LATE, EARLY)
        self.assertEqual(result.to_local, {"summary": None})


@override_settings(EVILFLOWERS_EXTERNAL_SYSTEMS=SYNC_SYSTEMS, CACHES=LOCMEM)
class SyncTestCase(HarvestTestCase):
    def setUp(self):
        FakeConnector.remote = FakeRemote()
        registry.register(FakeConnector)
        registry.register(ReadOnlyConnector)
        self.addCleanup(registry.unregister, "fake")
        self.addCleanup(registry.unregister, "readonly")
        # Outbound pushes the merge declares on commit are not what these tests look at.
        patcher = patch("apps.harvest.sync.SyncEngine._declare_push")
        self.declared = patcher.start()
        self.addCleanup(patcher.stop)
        self.remote = FakeConnector.remote

    def sync_source(self, **kwargs):
        values = {"slug": "fake-repo", "adapter": "fake", "system_key": "fake_id", "mode": "sync", "base_url": None}
        values.update(kwargs)
        return self.source(**values)

    def remote_book(self, record_id="F-1", **kwargs) -> SourceRecord:
        values = {
            "record_id": record_id,
            "title": "Numerická matematika",
            "authors": [SourceAuthor("Macák", "Marek")],
            "identifiers": {"isbn": "9788022712347"},
            "language": "sk",
            "publisher": "Spektrum STU",
            "published": "2018",
        }
        values.update(kwargs)
        return self.remote.put(SourceRecord(**values))

    def linked_entry(self, source):
        """An entry created from the remote record, in sync."""
        self.remote_book()
        apply_record(source, "F-1")
        return Entry.objects.get(identifiers__fake_id="F-1")


class InboundTests(SyncTestCase):
    def test_an_unknown_record_creates_an_entry_in_sync(self):
        source = self.sync_source()
        self.remote_book()

        outcome = apply_record(source, "F-1")

        self.assertEqual(outcome.status, "created")
        entry = Entry.objects.get(identifiers__fake_id="F-1")
        self.assertEqual(entry.origin, "harvest:fake-repo")
        self.assertEqual(entry.title, "Numerická matematika")
        row = HarvestRecord.objects.get(source=source, record_id="F-1")
        self.assertEqual(row.sync_state, "in_sync")
        self.assertEqual(row.synced_record["title"], "Numerická matematika")
        self.assertEqual(row.remote_version, self.remote.records["F-1"].version)

    def test_a_remote_edit_reaches_the_entry(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        self.remote.records["F-1"].publisher = "Iný vydavateľ"
        self.remote.records["F-1"].version = LATE.isoformat()

        outcome = apply_record(source, "F-1")

        entry.refresh_from_db()
        self.assertEqual(entry.publisher, "Iný vydavateľ")
        self.assertEqual(outcome.status, "enriched")
        self.assertEqual(HarvestRecord.objects.get(source=source, record_id="F-1").sync_state, "in_sync")

    def test_both_sides_changed_the_later_wins_and_the_loser_is_kept(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        Entry.objects.filter(pk=entry.pk).update(title="Lokálny názov", updated_at=EARLY)
        self.remote.records["F-1"].title = "Vzdialený názov"
        self.remote.records["F-1"].version = LATE.isoformat()

        apply_record(source, "F-1")

        entry.refresh_from_db()
        self.assertEqual(entry.title, "Vzdialený názov")
        conflict = HarvestConflict.objects.get(entry=entry)
        self.assertEqual(
            (conflict.field, conflict.local_value, conflict.remote_value, conflict.winner),
            ("title", "Lokálny názov", "Vzdialený názov", "remote"),
        )
        self.assertEqual(HarvestRecord.objects.get(source=source, record_id="F-1").sync_state, "conflict")

    def test_a_local_only_change_stays_pending_for_a_push(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        with syncing():
            Entry.objects.filter(pk=entry.pk).update(summary="Nový opis")

        with self.captureOnCommitCallbacks(execute=True):
            apply_record(source, "F-1")

        row = HarvestRecord.objects.get(source=source, record_id="F-1")
        self.assertEqual(row.sync_state, "pending")
        self.declared.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(entry.summary, "Nový opis")

    def test_a_sync_run_withdraws_entries_deleted_remotely(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        self.remote.feed = [SourceRecord(record_id="F-1", deleted=True)]

        report = run_source(source)

        entry.refresh_from_db()
        self.assertIsNotNone(entry.withdrawn_at)
        self.assertEqual(entry.identifiers["fake_id"], "F-1")  # the link stays
        self.assertEqual(report["deleted"], 1)
        self.assertFalse(Entry.objects.listed().filter(pk=entry.pk).exists())

    def test_a_sync_run_declares_pushes_for_unlinked_entries(self):
        source = self.sync_source()
        self.entry("Iba v Elvíre")
        self.remote.feed = []
        report = run_source(source)
        self.assertEqual(report["push_declared"], 1)


class OutboundTests(SyncTestCase):
    def test_push_then_the_remote_callback_writes_nothing(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        entry.title = "Opravený názov"
        entry.save()

        report = push(entry, source)

        self.assertEqual(report.pushed, ["title"])
        self.assertEqual(self.remote.records["F-1"].title, "Opravený názov")
        entry.refresh_from_db()
        before = entry.updated_at
        # The remote's webhook after our write: nothing differs from the base.
        outcome = apply_record(source, "F-1")
        entry.refresh_from_db()
        self.assertEqual(entry.updated_at, before)
        self.assertEqual(outcome.status, "matched")
        self.assertFalse(HarvestConflict.objects.exists())
        self.assertEqual(HarvestRecord.objects.get(source=source, record_id="F-1").sync_state, "in_sync")

    def test_a_stale_version_is_retried_once(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        entry.title = "Opravený názov"
        entry.save()
        self.remote.stale_times = 1

        report = push(entry, source)

        self.assertEqual(report.state, "in_sync")
        self.assertEqual(len(self.remote.pushes), 2)
        self.assertEqual(self.remote.records["F-1"].title, "Opravený názov")

    def test_stale_twice_is_an_error(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        entry.title = "Opravený názov"
        entry.save()
        self.remote.stale_times = 2

        report = push(entry, source)

        self.assertEqual(report.state, "error")
        self.assertEqual(HarvestRecord.objects.get(source=source, record_id="F-1").sync_state, "error")

    def test_an_unlinked_entry_is_created_remotely(self):
        source = self.sync_source()
        entry = self.entry("Iba v Elvíre", identifiers={"isbn": "9788022750783"})

        report = push(entry, source)

        self.assertTrue(report.created)
        entry.refresh_from_db()
        self.assertEqual(entry.identifiers["fake_id"], "F-1")
        self.assertEqual(entry.identifiers["fake_internal"], "#F-1")
        self.assertEqual(entry.origin, "elvira")
        self.assertEqual(self.remote.records["F-1"].title, "Iba v Elvíre")
        self.assertEqual(self.remote.records["F-1"].identifiers["isbn"], "9788022750783")
        # And the callback of that creation changes nothing.
        before = entry.updated_at
        apply_record(source, "F-1")
        entry.refresh_from_db()
        self.assertEqual(entry.updated_at, before)

    def test_entry_saves_enqueue_a_push_but_sync_writes_do_not(self):
        source = self.sync_source(enabled=True)
        with patch("apps.tasks.contract.declare") as declare:
            with self.captureOnCommitCallbacks(execute=True):
                entry = self.entry("Kniha")
                entry.title = "Kniha 2"
                entry.save()
            declare.assert_called_once_with("harvest.push", entry.pk, spec={"source": str(source.pk)})
            declare.reset_mock()
            from django.core.cache import cache

            from apps.harvest.signals import dedupe_key

            cache.delete(dedupe_key(source.pk, entry.pk))  # the push started
            with self.captureOnCommitCallbacks(execute=True):
                with syncing():
                    entry.title = "Kniha 3"
                    entry.save()
                entry.save(update_fields=["popularity"])
            declare.assert_not_called()

    def test_read_only_connectors_get_no_pushes(self):
        self.source(slug="ro", adapter="readonly", system_key="fake_id", mode="link_only")
        with patch("apps.tasks.contract.declare") as declare:
            with self.captureOnCommitCallbacks(execute=True):
                self.entry("Kniha")
        declare.assert_not_called()


class JobTests(SyncTestCase):
    def test_push_definition_checks(self):
        from apps.harvest.jobs import EntrySyncSpec, HarvestPullDefinition, HarvestPushDefinition
        from apps.tasks.domain.values import Blocked, Ready, Refused

        source = self.sync_source()
        entry = self.entry("Kniha")
        spec = EntrySyncSpec(source=source.pk)
        self.assertIsInstance(HarvestPushDefinition().check(entry, spec), Ready)
        self.assertIsInstance(HarvestPullDefinition().check(entry, spec), Refused)  # not linked
        HarvestSource.objects.filter(pk=source.pk).update(enabled=False)
        self.assertIsInstance(HarvestPushDefinition().check(entry, spec), Blocked)
        link_only = self.source(slug="arl")
        self.assertIsInstance(HarvestPushDefinition().check(entry, EntrySyncSpec(source=link_only.pk)), Refused)


class ApiTests(SyncTestCase):
    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def test_sync_mode_needs_a_writing_connector(self):
        payload = {
            "slug": "x",
            "name": "X",
            "adapter": "readonly",
            "system_key": "fake_id",
            "target_catalog_id": str(self.catalog.pk),
            "mode": "sync",
        }
        response = self.client.post(
            "/api/v1/harvest/sources",
            data=payload,
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(response.status_code, 422, response.content)
        payload["adapter"] = "fake"
        response = self.client.post(
            "/api/v1/harvest/sources",
            data=payload,
            content_type="application/json",
            headers=self.headers(self.manager),
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["response"]["oai_reexpose"])

    def test_connectors_list_their_capabilities(self):
        response = self.client.get("/api/v1/harvest/connectors", headers=self.headers(self.manager))
        items = {item["name"]: item for item in response.json()["response"]}
        self.assertEqual(items["fake"]["capabilities"], ["read", "write", "validate", "link"])
        self.assertEqual(items["fake"]["config_schema"]["type"], "object")
        self.assertEqual(items["oai_pmh"]["capabilities"], ["read"])

    def test_entry_sync_state(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        Entry.objects.filter(pk=entry.pk).update(identifiers={**entry.identifiers, "arl_stu": "0000101"})
        self.source(slug="arl-stu", system_key="arl_stu")

        response = self.client.get(f"/api/v1/entries/{entry.pk}/sync", headers=self.headers(self.manager))

        self.assertEqual(response.status_code, 200, response.content)
        sources = {item["slug"]: item for item in response.json()["response"]["sources"]}
        self.assertEqual(sources["fake-repo"]["record_url"], "https://fake.example.org/records/F-1")
        self.assertEqual(sources["fake-repo"]["sync_state"], "in_sync")
        self.assertEqual(sources["fake-repo"]["open_conflicts"], 0)
        # A link-only catalogue record, with the configured external system's URL.
        self.assertEqual(sources["arl-stu"]["record_url"], "https://kis.example.org/detail-0000101/")
        self.assertIsNone(sources["arl-stu"]["sync_state"])

        self.assertEqual(
            self.client.get(f"/api/v1/entries/{entry.pk}/sync", headers=self.headers(self.outsider)).status_code, 404
        )

    def test_conflicts_list_and_acknowledge(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        Entry.objects.filter(pk=entry.pk).update(title="L", updated_at=EARLY)
        self.remote.records["F-1"].title = "R"
        self.remote.records["F-1"].version = LATE.isoformat()
        apply_record(source, "F-1")

        response = self.client.get(
            f"/api/v1/harvest/conflicts?entry_id={entry.pk}&acknowledged=false", headers=self.headers(self.manager)
        )
        self.assertEqual(response.status_code, 200, response.content)
        items = response.json()["items"]
        self.assertEqual([(i["field"], i["winner"]) for i in items], [("title", "remote")])

        url = f"/api/v1/harvest/conflicts/{items[0]['id']}/acknowledge"
        self.assertEqual(self.client.post(url, headers=self.headers(self.outsider)).status_code, 404)
        response = self.client.post(url, headers=self.headers(self.manager))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["response"]["acknowledged"])
        response = self.client.get("/api/v1/harvest/conflicts?acknowledged=false", headers=self.headers(self.manager))
        self.assertEqual(response.json()["metadata"]["total"], 0)


@override_settings(EVILFLOWERS_PORTAL_URL="https://portal.example.org")
class ResolverTests(SyncTestCase):
    def test_redirects_to_the_portal_entry(self):
        source = self.sync_source()
        entry = self.linked_entry(source)
        with patch("apps.harvest.resolve.settings_store.get", return_value=""):
            response = self.client.get("/resolve/fake-repo?id=F-1")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], f"https://portal.example.org/library?entry-detail-id={entry.pk}")
            self.assertEqual(self.client.get("/resolve/fake-repo?pid=F-1").status_code, 302)
            self.assertEqual(self.client.get("/resolve/fake-repo?id=nope").status_code, 404)
            self.assertEqual(self.client.get("/resolve/unknown?id=F-1").status_code, 404)
            Entry.objects.filter(pk=entry.pk).update(withdrawn_at=LATE)
            self.assertEqual(self.client.get("/resolve/fake-repo?id=F-1").status_code, 404)


class ExposureTests(SyncTestCase):
    def test_not_reexposed_origins(self):
        self.sync_source(oai_reexpose=False)
        self.source(slug="arl-stu")
        self.assertEqual(not_reexposed_origins(), {"harvest:fake-repo"})


class McpTests(SyncTestCase):
    def call(self, name, arguments, user):
        from django.test import RequestFactory

        from apps.mcp.protocol import LATEST_PROTOCOL_VERSION
        from apps.mcp.server import McpServer
        from apps.mcp.tools import registry as tools

        request = RequestFactory().post("/mcp/v1")
        request.user = user
        return McpServer(tools).handle(
            request,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
            LATEST_PROTOCOL_VERSION,
        )["result"]

    def test_get_entry_sync_and_push_entry(self):
        from unittest.mock import MagicMock

        source = self.sync_source()
        entry = self.linked_entry(source)
        result = self.call("get_entry_sync", {"entry_id": str(entry.pk)}, self.manager)
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["sources"][0]["record_id"], "F-1")
        self.assertTrue(self.call("get_entry_sync", {"entry_id": str(entry.pk)}, self.outsider).get("isError"))

        job = MagicMock(pk="00000000-0000-0000-0000-000000000002", state="queued", reason=None)
        with patch("apps.mcp.tools.harvest.declare", return_value=job) as declare:
            result = self.call("push_entry", {"entry_id": str(entry.pk)}, self.manager)
        self.assertFalse(result.get("isError"), result)
        declare.assert_called_once_with("harvest.push", entry.pk, spec={"source": str(source.pk)})


class NoSystemNamesTests(SimpleTestCase):
    def test_apps_harvest_names_no_system(self):
        root = Path(__file__).resolve().parents[1]
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in (".py", ".xml", ".json", ".md", ".txt")
            and "migrations" not in path.parts
            and "data" + "verse" in path.read_text(errors="ignore").lower()
        ]
        self.assertEqual(offenders, [])

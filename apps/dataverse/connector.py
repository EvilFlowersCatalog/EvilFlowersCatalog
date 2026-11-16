"""IP-034: Dataverse as a harvest connector (`HarvestSource.adapter = "dataverse"`).

The generic sync (`apps.harvest`) does matching, merging, conflicts, withdrawal and scheduling; this class only
speaks Dataverse's native API:

- **read:** a pass lists datasets of the configured collection newest change first (Search API, `sort=date`) down
  to the cursor and fetches each one's `:latest-published` version (Q4). It then checks a rotating batch of linked
  datasets (all of them on a full pass) with `versions?includeDeaccessioned=true`: deaccessioned or 404 is
  reported as a deleted record (Q6). A pass is never `exhaustive`: an empty or failing search must not read as
  "everything is gone". At most `max_withdrawals` deletions per pass.
- **write:** `editMetadata?replace=true` with only the changed fields and `sourceLastUpdateTime`, then
  `publish?type=updatecurrent` (Q5), with the superuser token. A new record is a `book` dataset in
  `config.collection`, published as a major version. Files are never uploaded (Q8).
- **validate:** the Q9 rules (`mapper.book_problems`); **link:** the dataset page.

Tokens come from settings, never from the source's config: reads use `EVILFLOWERS_DATAVERSE_API_TOKEN`, writes
`EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN` (empty: the connector is read-only).
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.core.models import Entry
from apps.dataverse.exceptions import (
    DataverseConfigError,
    DataverseError,
    DataverseNotFoundError,
    DataverseStaleError,
)
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.services.links import portal_entry_url
from apps.dataverse.services.mapper import (
    DATASET_TYPE_BOOK,
    book_problems,
    changed_values,
    create_payload,
    dataset_to_source_record,
    edit_payload,
    lending_of,
    record_to_values,
)
from apps.harvest.contract import Connector, PushResult, SourceRecord, StaleVersion, registry

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
#: Linked datasets whose existence an incremental pass checks (a full pass checks all).
DEFAULT_CHECK_BATCH = 50
#: Deletions one pass reports at most: a Dataverse redeployed empty would otherwise withdraw the whole catalog.
DEFAULT_MAX_WITHDRAWALS = 20


# -- cursor: "<newest search date>|<offset of the next existence check>" (HarvestSource.cursor is 40 chars) ----


def parse_cursor(cursor: Optional[str]) -> tuple[Optional[str], int]:
    if not cursor:
        return None, 0
    since, _, offset = cursor.partition("|")
    return since or None, int(offset) if offset.isdigit() else 0


def format_cursor(since: Optional[str], offset: int) -> str:
    return f"{since or ''}|{offset}"


def _time(value: Optional[str]):
    return parse_datetime(value) if value else None


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    ta, tb = _time(a), _time(b)
    if ta is None or tb is None:
        return max(a, b)
    return a if ta >= tb else b


class DataverseReader:
    exhaustive = False

    def __init__(self, connector: "DataverseConnector", source, *, full: bool, client: DataverseClient):
        self.connector = connector
        self.source = source
        self.full = full
        self.client = client
        config = source.config or {}
        self.subtree = config.get("subtree") or config.get("collection") or None
        self.check_batch = int(config.get("check_batch") or DEFAULT_CHECK_BATCH)
        self.max_withdrawals = int(config.get("max_withdrawals") or DEFAULT_MAX_WITHDRAWALS)
        self.since, self.offset = parse_cursor(source.cursor)
        if full:
            self.since = None
        self.cursor: Optional[str] = None
        self.seen: set[str] = set()

    def records(self) -> Iterator[SourceRecord]:
        newest = self.since
        start = 0
        while True:
            page = self.client.search_datasets(start=start, per_page=PAGE_SIZE, subtree=self.subtree)
            items = page.get("items") or []
            stop = False
            for item in items:
                pid = item.get("global_id")
                stamp = item.get("updatedAt") or item.get("published_at")
                if self.since and stamp and _time(stamp) and _time(stamp) <= _time(self.since):
                    stop = True
                    break
                if not pid or pid in self.seen:
                    continue
                self.seen.add(pid)
                record = self.connector.fetch(self.source, pid, client=self.client)
                newest = _later(newest, stamp or (record.version if record is not None else None))
                if record is not None:
                    yield record
            start += len(items)
            if stop or not items or start >= int(page.get("total_count") or 0):
                break
        yield from self._deletions()
        self.cursor = format_cursor(newest, self.offset)

    def _linked(self) -> list[str]:
        key = self.source.system_key
        rows = (
            Entry.objects.filter(
                catalog_id=self.source.target_catalog_id, withdrawn_at__isnull=True, identifiers__has_key=key
            )
            .values_list("identifiers", flat=True)
            .order_by("pk")
        )
        return sorted({(identifiers or {}).get(key) for identifiers in rows} - {None, ""} - self.seen)

    def _deletions(self) -> Iterator[SourceRecord]:
        linked = self._linked()
        if not linked:
            self.offset = 0
            return
        if self.full:
            batch = linked
            self.offset = 0
        else:
            start = self.offset % len(linked)
            batch = (linked[start:] + linked[:start])[: self.check_batch]
            self.offset = (start + len(batch)) % len(linked)
        gone = 0
        for pid in batch:
            reason = self.connector.gone(self.client, pid)
            if reason is None:
                continue
            if gone >= self.max_withdrawals:
                logger.warning(
                    "dataverse.reader source=%s stopped after %d deletions in one pass (max_withdrawals); "
                    "check the Dataverse before the next run",
                    self.source.slug,
                    gone,
                )
                return
            gone += 1
            logger.warning("dataverse.reader source=%s pid=%s gone: %s", self.source.slug, pid, reason)
            yield SourceRecord(record_id=pid, deleted=True, extra={"gone": reason})

    def probe(self) -> dict:
        errors, sample, total = [], None, None
        try:
            page = self.client.search_datasets(start=0, per_page=1, subtree=self.subtree)
            total = page.get("total_count")
            items = page.get("items") or []
            if items and items[0].get("global_id"):
                record = self.connector.fetch(self.source, items[0]["global_id"], client=self.client)
                sample = record.as_dict() if record is not None else None
        except DataverseError as error:
            errors.append(str(error))
        return {
            "identify": {"base_url": self.client.base_url, "collection": self.subtree, "datasets": total},
            "metadata_formats": ["dataverse_json"],
            "sets": [self.subtree] if self.subtree else [],
            "sample": sample,
            "errors": errors,
        }


@registry.register
class DataverseConnector(Connector):
    name = "dataverse"
    title = "Dataverse"
    description = (
        "A Dataverse 6.4+ installation over its native API: published versions in, `book` datasets out "
        "(editMetadata + publish updatecurrent with the superuser token), deaccessions withdraw entries."
    )
    config_schema = {
        "type": "object",
        "required": ["collection"],
        "properties": {
            "collection": {"type": "string", "description": "Alias of the Dataverse collection new datasets go to."},
            "subtree": {"type": "string", "description": "Collection alias a pass lists (default: `collection`)."},
            "check_batch": {"type": "integer", "minimum": 1, "description": "Linked datasets checked per pass."},
            "max_withdrawals": {"type": "integer", "minimum": 0, "description": "Deletions reported per pass."},
        },
        "additionalProperties": False,
    }
    system_keys = ("dataverse_pid",)

    # -- clients -----------------------------------------------------------------------------------------------

    def reader_client(self) -> DataverseClient:
        token = settings.EVILFLOWERS_DATAVERSE_API_TOKEN
        if not token:
            raise DataverseConfigError("DATAVERSE_API_TOKEN is missing")
        return DataverseClient(settings.EVILFLOWERS_DATAVERSE_BASE_INTERNAL, token)

    def writer_client(self) -> DataverseClient:
        token = getattr(settings, "EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN", "")
        if not token:
            raise DataverseConfigError("EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN is missing: outbound is off")
        return DataverseClient(settings.EVILFLOWERS_DATAVERSE_BASE_INTERNAL, token)

    # -- read --------------------------------------------------------------------------------------------------

    def reader(self, source, *, full: bool = False) -> DataverseReader:
        return DataverseReader(self, source, full=full, client=self.reader_client())

    def fetch(self, source, record_id: str, *, client: Optional[DataverseClient] = None) -> Optional[SourceRecord]:
        """The dataset's latest published version; `deleted=True` when it is deaccessioned or gone (404). A
        dataset that was never published raises: it is not gone, there is just nothing to read yet."""
        client = client or self.reader_client()
        try:
            version = client.get_version_by_pid(record_id, ":latest-published")
        except DataverseNotFoundError:
            reason = self.gone(client, record_id)
            if reason is None:
                raise DataverseError(f"dataset {record_id} has no published version")
            return SourceRecord(record_id=record_id, deleted=True, extra={"gone": reason})
        return dataset_to_source_record(version or {}, pid=record_id)

    @staticmethod
    def gone(client: DataverseClient, pid: str) -> Optional[str]:
        """Why the dataset no longer counts ("deaccessioned", "not found"), or None while a version is published.
        Only a 404 or an explicit deaccession counts; auth, 5xx and network errors raise."""
        try:
            versions = client.list_versions(pid, include_deaccessioned=True)
        except DataverseNotFoundError:
            return "not found"
        states = {str(version.get("versionState") or "").upper() for version in versions}
        if "RELEASED" in states:
            return None
        if "DEACCESSIONED" in states:
            return "deaccessioned"
        return None  # a draft only: never published, not gone

    # -- write -------------------------------------------------------------------------------------------------

    def writable(self, source=None) -> bool:
        return bool(getattr(settings, "EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN", ""))

    def _owned(self, source, record_id: Optional[str], record: SourceRecord) -> dict:
        """The `elvira_book` values only Elvira writes, from the entry the record belongs to."""
        entry_id = record.extra.get("entry_id")
        entry = None
        if entry_id:
            entry = Entry.objects.filter(pk=entry_id).select_related("catalog").first()
        elif record_id and source is not None:
            entry = (
                Entry.objects.filter(
                    catalog_id=source.target_catalog_id, **{f"identifiers__{source.system_key}": record_id}
                )
                .select_related("catalog")
                .first()
            )
        if entry is None:
            return {}
        return {
            "elvira_entry_id": str(entry.pk),
            "elvira_url": portal_entry_url(entry.pk),
            "lending": lending_of(entry),
        }

    def push(
        self, source, record_id: Optional[str], record: SourceRecord, expected_version: Optional[str]
    ) -> PushResult:
        client = self.writer_client()
        wanted = {**record_to_values(record), **self._owned(source, record_id, record)}
        if record_id is None:
            return self._create(source, client, record, wanted)

        dataset = client.get_dataset_by_pid(record_id) or {}
        latest = dataset.get("latestVersion") or {}
        dataset_id = str(dataset.get("id") or "")
        state = str(latest.get("versionState") or "").upper()
        never_published = state == "DRAFT" and latest.get("versionNumber") is None
        if state == "DRAFT" and not never_published:
            # `updatecurrent` would publish someone's unfinished edits along with ours.
            raise DataverseError(f"a draft of {record_id} is open in Dataverse; publish or discard it first")
        if state == "DEACCESSIONED":
            raise DataverseError(f"{record_id} is deaccessioned in Dataverse")
        if expected_version and not never_published and latest.get("lastUpdateTime") != expected_version:
            raise StaleVersion(f"{record_id} changed: {latest.get('lastUpdateTime')} != {expected_version}")

        remote = dataset_to_source_record(latest, pid=record_id, dataset_id=dataset_id)
        changed = changed_values(wanted, remote)
        identifiers = {"dataverse_dataset_id": dataset_id} if dataset_id else {}
        if not changed and not never_published:
            return PushResult(record_id=record_id, version=remote.version, identifiers=identifiers)

        to_set, to_clear = edit_payload(wanted, changed, remote)
        try:
            if to_set:
                client.edit_metadata(record_id, to_set, remote.version)
        except DataverseStaleError as error:
            raise StaleVersion(str(error)) from error
        if to_clear:
            client.delete_metadata(record_id, to_clear)
        client.publish(record_id, "major" if never_published else "updatecurrent")
        published = client.get_version_by_pid(record_id, ":latest-published") or {}
        logger.info("dataverse.push pid=%s fields=%s", record_id, ",".join(changed) or "-")
        return PushResult(record_id=record_id, version=published.get("lastUpdateTime"), identifiers=identifiers)

    def _create(self, source, client: DataverseClient, record: SourceRecord, wanted: dict) -> PushResult:
        alias = ((source.config or {}).get("collection") or "").strip() if source is not None else ""
        if not alias:
            raise DataverseConfigError("The source has no `config.collection` to create datasets in.")
        email = getattr(settings, "EVILFLOWERS_DATAVERSE_CONTACT_EMAIL", "") or getattr(
            settings, "EVILFLOWERS_CONTACT_EMAIL", ""
        )
        if not email:
            raise DataverseConfigError("No contact e-mail for new datasets (EVILFLOWERS_DATAVERSE_CONTACT_EMAIL).")
        name = source.target_catalog.title if getattr(source, "target_catalog_id", None) else source.name
        licence = {"name": record.licence_name, "uri": record.licence_url}
        created = client.create_dataset(
            alias, create_payload(wanted, contact_email=email, contact_name=name, licence=licence)
        )
        pid, dataset_id = created.get("persistentId"), created.get("id")
        if not pid:
            raise DataverseError(f"Dataverse created no persistent id: {created}")
        identifiers = {"dataverse_dataset_id": str(dataset_id)} if dataset_id else {}
        version = None
        try:
            client.publish(pid, "major")
            version = (client.get_version_by_pid(pid, ":latest-published") or {}).get("lastUpdateTime")
        except DataverseError as error:
            # The dataset exists: return its id so the entry is linked (no duplicate on the next push). It stays
            # a draft; the next push publishes it.
            logger.error("dataverse.create pid=%s created but not published: %s", pid, error)
        logger.info("dataverse.created pid=%s collection=%s", pid, alias)
        return PushResult(record_id=pid, version=version, identifiers=identifiers)

    # -- validate, link ----------------------------------------------------------------------------------------

    def validate(self, source, record: SourceRecord) -> list[str]:
        kind = record.extra.get("dataset_type")
        if kind not in (None, DATASET_TYPE_BOOK):
            return []
        return book_problems(record)

    def record_url(self, source, record_id: str) -> Optional[str]:
        base = (settings.EVILFLOWERS_DATAVERSE_PUBLIC_BASE or "").rstrip("/")
        if not base or not record_id:
            return None
        return f"{base}/dataset.xhtml?{urlencode({'persistentId': record_id})}"

    def validate_config(self, config: dict) -> list[str]:
        problems = []
        if not isinstance(config.get("collection"), str) or not config["collection"].strip():
            problems.append("`config.collection` (the Dataverse collection alias) is required.")
        unknown = sorted(set(config) - set(self.config_schema["properties"]))
        if unknown:
            problems.append(f"Unknown `config` keys: {', '.join(unknown)}.")
        return problems


__all__ = ["DataverseConnector", "DataverseReader", "format_cursor", "parse_cursor"]

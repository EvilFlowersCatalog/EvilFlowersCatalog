"""Generic two-way sync (IP-034 Part B) for sources in mode `sync`, with any connector that can write.

- **Inbound** (`apply_record`, and every record of a `harvest.run`): fetch → match/link, or create as in
  `create` mode → three-way merge against `HarvestRecord.synced_record` → write the entry through the applier →
  store the new base, versions and state. A record gone remotely **withdraws** its entry (Q6); the link stays.
- **Outbound** (`push`, the `harvest.push` job): fetch the remote record if linked, merge, write the remote
  values the entry lacks, then `connector.push(record_id, merged, expected_version)`; a `StaleVersion` re-fetches,
  re-merges and retries once. An entry without a remote record is created remotely (`record_id=None`): the new id
  goes to `identifiers[system_key]` with the identifiers the connector returns; `origin` stays as it is.
- **Loop guard:** after a push, the base and both versions take the pushed state, so the remote's own callback
  (a webhook after our write) merges to no change and writes nothing.

Conflicts (both sides changed a field since the base) are decided by the later side and kept as
`HarvestConflict`s.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from django.db import connection, transaction
from django.utils import timezone

from apps.core.identifiers import allowed_identifier_keys
from apps.core.models import Entry
from apps.harvest.adapters import connector_for
from apps.harvest.adapters.registry import StaleVersion
from apps.harvest.apply import Applier, Outcome, _datestamp, run_user, store_record
from apps.harvest.matching import MatchIndex
from apps.harvest.merge import merge
from apps.harvest.models import HarvestRecord, HarvestSource, SyncState
from apps.harvest.projection import SYNC_FIELDS, from_entry, from_record, project, to_record
from apps.harvest.records import SourceRecord
from apps.harvest.signals import syncing

logger = logging.getLogger(__name__)

Status = HarvestRecord.Status


class SyncError(Exception):
    """The sync cannot go on for this entry and source (not a sync source, read-only connector, record gone)."""


@dataclass
class PushReport:
    record_id: Optional[str]
    state: str
    created: bool = False
    pushed: list = None
    pulled: list = None
    conflicts: int = 0
    message: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "state": self.state,
            "created": self.created,
            "pushed": self.pushed or [],
            "pulled": self.pulled or [],
            "conflicts": self.conflicts,
            "message": self.message,
        }


def remote_time(record: Optional[SourceRecord]):
    """When the remote record last changed, for last-write-wins: its version if that is a time, else its
    datestamp."""
    if record is None:
        return None
    return _datestamp(record.version) or _datestamp(record.datestamp)


def _record_lock(source: HarvestSource, record_id: str) -> None:
    """One merge per record at a time (a webhook and a run may meet); held until the transaction ends."""
    digest = hashlib.sha256(f"harvest:{source.pk}:{record_id}".encode()).digest()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest[:8], "big", signed=True)])


class SyncEngine:
    def __init__(self, source: HarvestSource, *, index=None, applier=None, dry_run: bool = False):
        if source.mode != HarvestSource.Mode.SYNC:
            raise SyncError(f"Source '{source.slug}' is not in sync mode.")
        self.source = source
        self.connector = connector_for(source)
        self.dry_run = dry_run
        self.index = index or MatchIndex(source.target_catalog, source.system_key)
        self.applier = applier or Applier(source, self.index, user=run_user(source), dry_run=dry_run)

    # -- inbound ---------------------------------------------------------------------------------------------

    def inbound(self, record: SourceRecord, row: Optional[HarvestRecord] = None) -> Outcome:
        if row is None and not self.dry_run:
            row = HarvestRecord.objects.filter(source=self.source, record_id=record.record_id, dry_run=False).first()
        if record.deleted:
            return self.withdraw(record.record_id)
        match = self.index.match(record)
        if match.status == "ambiguous":
            return Outcome(
                Status.AMBIGUOUS, matched_on=match.matched_on, message=match.message, sync={"sync_state": "error"}
            )
        if match.status == "unmatched":
            with syncing():
                outcome = self.applier.create(record)
            if outcome.status == Status.CREATED and not self.dry_run:
                outcome.entry.refresh_from_db()
                outcome.sync = self._state(from_entry(outcome.entry), record, outcome.entry, SyncState.IN_SYNC)
            return outcome

        entry = Entry.objects.select_related("language").get(pk=match.entry_id)
        local, remote = from_entry(entry), from_record(record)
        base = row.synced_record if row is not None else None
        result = merge(base, local, remote, entry.updated_at, remote_time(record), SYNC_FIELDS)
        status = Status.ENRICHED if result.to_local else Status.MATCHED
        message = self._describe(result)
        if self.dry_run:
            return Outcome(status, entry, match.matched_on, message=message, changed=sorted(result.to_local))
        with syncing():
            self.applier.link(entry, record.record_id)
            self.applier.apply_values(entry, result.to_local)
        self.index.link(str(entry.pk), record.record_id)
        entry.refresh_from_db()
        # Fields only the entry changed are not on the remote yet: their base stays the remote's value, so the
        # push that follows sees them as local changes.
        new_base = {**result.merged, **{name: remote.get(name) for name in result.to_remote}}
        if result.to_remote:
            state = SyncState.PENDING
            transaction.on_commit(lambda: self._declare_push(entry.pk))
        else:
            state = SyncState.CONFLICT if result.conflicts else SyncState.IN_SYNC
        return Outcome(
            status,
            entry,
            match.matched_on,
            message=message,
            changed=sorted(result.to_local),
            sync=self._state(new_base, record, entry, state),
            conflicts=result.conflicts,
            conflict_times=(entry.updated_at, remote_time(record)),
        )

    def withdraw(self, record_id: str) -> Outcome:
        """Deleted remotely: the entry is withdrawn (hidden, not borrowable; running loans end normally)."""
        holders = sorted(self.index.holders(record_id))
        entry = None
        for entry_id in holders:
            entry = Entry.objects.get(pk=entry_id)
            if entry.withdrawn_at is None and not self.dry_run:
                entry.withdrawn_at = timezone.now()
                with syncing():
                    entry.save(update_fields=["withdrawn_at", "updated_at", "touched_at"])
        message = f"withdrew {len(holders)} entr{'y' if len(holders) == 1 else 'ies'}" if holders else None
        return Outcome(Status.DELETED, entry, message=message, sync={"sync_state": SyncState.IN_SYNC})

    # -- outbound --------------------------------------------------------------------------------------------

    def push(self, entry: Entry) -> PushReport:
        if not self.connector.can_write(self.source):
            raise SyncError(f"The connector of source '{self.source.slug}' cannot write.")
        if entry.catalog_id != self.source.target_catalog_id:
            raise SyncError("The entry is not in the source's target catalog.")
        record_id = (entry.identifiers or {}).get(self.source.system_key)
        if not record_id:
            return self._create_remote(entry)
        for attempt in (1, 2):
            try:
                return self._push_linked(entry, record_id)
            except StaleVersion:
                if attempt == 2:
                    row = self._row(record_id)
                    self._save_state(row, SyncState.ERROR, "the remote record kept changing; push retried once")
                    return PushReport(record_id, SyncState.ERROR, message="stale version twice")
                logger.info("harvest.push_stale source=%s record=%s retrying", self.source.slug, record_id)
                entry.refresh_from_db()

    def _push_linked(self, entry: Entry, record_id: str) -> PushReport:
        remote_record = self.connector.fetch(self.source, record_id)
        row = self._row(record_id)
        if remote_record is None or remote_record.deleted:
            self._save_state(row, SyncState.ERROR, "the remote record is gone")
            return PushReport(record_id, SyncState.ERROR, message="the remote record is gone")
        local, remote = from_entry(entry), from_record(remote_record)
        base = row.synced_record if row is not None else None
        result = merge(base, local, remote, entry.updated_at, remote_time(remote_record), SYNC_FIELDS)
        with syncing():
            self.applier.apply_values(entry, result.to_local)
        version = remote_record.version
        sent = remote_record
        if result.to_remote:
            sent = to_record(result.merged, record_id=record_id, scheme=self.source.slug, template=remote_record)
            pushed = self.connector.push(self.source, record_id, sent, remote_record.version)
            version = pushed.version
            sent.version = version
            self._link_identifiers(entry, pushed.identifiers)
        entry.refresh_from_db()
        state = SyncState.CONFLICT if result.conflicts else SyncState.IN_SYNC
        store_record(
            self.source,
            sent,
            Outcome(
                Status.ENRICHED if result.to_local else Status.MATCHED,
                entry,
                self.source.system_key,
                message=self._describe(result),
                sync={
                    "synced_record": result.merged,
                    "remote_version": version,
                    "local_version": entry.updated_at,
                    "sync_state": state,
                },
                conflicts=result.conflicts,
                conflict_times=(entry.updated_at, remote_time(remote_record)),
            ),
            dry_run=False,
        )
        return PushReport(
            record_id,
            state,
            pushed=sorted(result.to_remote),
            pulled=sorted(result.to_local),
            conflicts=len(result.conflicts),
        )

    def _create_remote(self, entry: Entry) -> PushReport:
        local = from_entry(entry)
        outgoing = project(entry, self.source)
        pushed = self.connector.push(self.source, None, outgoing, None)
        outgoing.record_id = pushed.record_id
        outgoing.version = pushed.version
        self._link_identifiers(entry, {**pushed.identifiers, self.source.system_key: pushed.record_id})
        self.index.link(str(entry.pk), pushed.record_id)
        entry.refresh_from_db()
        store_record(
            self.source,
            outgoing,
            Outcome(
                Status.MATCHED,
                entry,
                "push",
                message="created remotely",
                sync={
                    "synced_record": local,
                    "remote_version": pushed.version,
                    "local_version": entry.updated_at,
                    "sync_state": SyncState.IN_SYNC,
                },
            ),
            dry_run=False,
        )
        return PushReport(pushed.record_id, SyncState.IN_SYNC, created=True, pushed=sorted(local))

    def _link_identifiers(self, entry: Entry, identifiers: dict) -> None:
        allowed = allowed_identifier_keys() | {self.source.system_key}
        wanted = {key: str(value) for key, value in (identifiers or {}).items() if key in allowed and value}
        merged = {**(entry.identifiers or {}), **wanted}
        if merged != (entry.identifiers or {}):
            entry.identifiers = merged
            with syncing():
                entry.save(update_fields=["identifiers", "updated_at", "touched_at"])

    # -- helpers ---------------------------------------------------------------------------------------------

    def declare_unlinked(self) -> int:
        """Entries of the target catalog with no remote record yet: created remotely by `harvest.push`."""
        if not self.connector.can_write(self.source):
            return 0
        key = self.source.system_key
        entry_ids = list(
            Entry.objects.listed()
            .filter(catalog_id=self.source.target_catalog_id)
            .exclude(identifiers__has_key=key)
            .values_list("pk", flat=True)
        )
        for entry_id in entry_ids:
            transaction.on_commit(lambda entry_id=entry_id: self._declare_push(entry_id))
        return len(entry_ids)

    def _declare_push(self, entry_id) -> None:
        from apps.tasks.contract import DispatchError, declare

        try:
            declare("harvest.push", entry_id, spec={"source": str(self.source.pk)})
        except DispatchError as error:
            logger.warning("harvest.push_refused entry=%s source=%s: %s", entry_id, self.source.slug, error)

    def _row(self, record_id: str) -> Optional[HarvestRecord]:
        return HarvestRecord.objects.filter(source=self.source, record_id=record_id, dry_run=False).first()

    @staticmethod
    def _save_state(row: Optional[HarvestRecord], state: str, message: str) -> None:
        if row is not None:
            row.sync_state, row.message = state, message
            row.save(update_fields=["sync_state", "message", "updated_at"])

    def _state(self, base: dict, record: SourceRecord, entry: Entry, state: str) -> dict:
        return {
            "synced_record": base,
            "remote_version": record.version,
            "local_version": entry.updated_at,
            "sync_state": state,
        }

    @staticmethod
    def _describe(result) -> Optional[str]:
        parts = []
        if result.to_local:
            parts.append("from remote: " + ", ".join(sorted(result.to_local)))
        if result.to_remote:
            parts.append("to remote: " + ", ".join(sorted(result.to_remote)))
        if result.conflicts:
            parts.append("conflicts: " + ", ".join(conflict.field for conflict in result.conflicts))
        return "; ".join(parts) or None


# -- entry points --------------------------------------------------------------------------------------------


def apply_record(source: HarvestSource, record_id: str, *, record: Optional[SourceRecord] = None) -> Outcome:
    """Bring one remote record in (a webhook, `harvest.pull`): fetched unless given, then matched and applied by
    the source's mode (`sync`: merged). Returns what was decided; the mirror row is stored."""
    connector = connector_for(source)
    with transaction.atomic():
        _record_lock(source, record_id)
        remote = record if record is not None else connector.fetch(source, record_id)
        if remote is None:
            remote = SourceRecord(record_id=record_id, deleted=True)
        if source.mode == HarvestSource.Mode.SYNC:
            outcome = SyncEngine(source).inbound(remote)
        else:
            index = MatchIndex(source.target_catalog, source.system_key)
            applier = Applier(source, index, user=run_user(source))
            outcome = applier.remove(remote.record_id) if remote.deleted else applier.apply(remote)
        store_record(source, remote, outcome, dry_run=False)
    return outcome


def push(entry: Entry, source: HarvestSource) -> PushReport:
    """Send the entry's state to the remote record of `source` (created when the entry has none)."""
    with transaction.atomic():
        record_id = (entry.identifiers or {}).get(source.system_key)
        if record_id:
            _record_lock(source, record_id)
        return SyncEngine(source).push(entry)


def pull(entry: Entry, source: HarvestSource) -> Outcome:
    record_id = (entry.identifiers or {}).get(source.system_key)
    if not record_id:
        raise SyncError("The entry is not linked to a record of this source.")
    return apply_record(source, record_id)


__all__ = ["PushReport", "SyncEngine", "SyncError", "apply_record", "pull", "push", "remote_time"]

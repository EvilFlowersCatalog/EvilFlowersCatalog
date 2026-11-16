"""IP-033: keep `oai_records` in step with the catalog; the fingerprint decides the datestamp.

`sync(entry_ids)` re-projects each entry and writes only when the fingerprint changed (or a tombstone comes back
to life), so a read that saves the entry never moves a datestamp. An entry that is gone or no longer exposed
leaves a tombstone. Every write is one statement under autocommit (Celery and the management command run
outside `atomic()`), and the datestamp is the database clock at that statement, so a harvester that reuses the
previous `responseDate` as its next `from` does not skip the change.

The clock is `statement_timestamp()`, not `now()`: identical under autocommit, but `now()` would freeze at the
start of an enclosing transaction (a test, a shell session) and stamp every write with the same second.
"""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Iterable, Iterator

from django.db import connection
from django.db.models.expressions import RawSQL

from apps.core.models import Entry
from apps.oai import projection as projector
from apps.oai.models import OaiRecord

logger = logging.getLogger(__name__)

#: Entries projected per round of queries.
BATCH_SIZE = 200

DATESTAMP_SQL = "date_trunc('second', statement_timestamp())"

# One statement: insert, or overwrite when the record changed or comes back from a tombstone. The WHERE also
# keeps two concurrent syncs of one entry from stamping a record twice for the same content.
_UPSERT = f"""
INSERT INTO oai_records (entry_id, set_specs, datestamp, deleted, fingerprint, projection)
VALUES (%s::uuid, %s::text[],{DATESTAMP_SQL}, false, %s, %s::jsonb)
ON CONFLICT (entry_id) DO UPDATE SET
    set_specs = EXCLUDED.set_specs,
    datestamp = EXCLUDED.datestamp,
    deleted = false,
    fingerprint = EXCLUDED.fingerprint,
    projection = EXCLUDED.projection
WHERE oai_records.fingerprint <> EXCLUDED.fingerprint OR oai_records.deleted
"""


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    #: Never exposed and still not: no row is created for them.
    skipped: int = 0

    def merge(self, other: "SyncResult") -> "SyncResult":
        for name, value in asdict(other).items():
            setattr(self, name, getattr(self, name) + value)
        return self

    def as_dict(self) -> dict:
        return asdict(self)


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class OaiRecordSync:
    def __init__(self, dry_run: bool = False):
        #: Count what would change without writing.
        self.dry_run = dry_run

    def sync(self, entry_ids: Iterable) -> SyncResult:
        ids = sorted({str(entry_id) for entry_id in entry_ids if entry_id})
        result = SyncResult()
        for chunk in _chunks(ids, BATCH_SIZE):
            result.merge(self._sync_chunk(chunk))
        return result

    def reconcile(self) -> SyncResult:
        """Every entry of a harvestable catalog, plus every live row (its entry may be gone or hidden now).

        Catches what fires no signal: `QuerySet.update()`, bulk operations, settings changes, crosswalk upgrades.
        """
        candidates = {str(pk) for pk in Entry.objects.filter(catalog__harvestable=True).values_list("pk", flat=True)}
        candidates |= {str(pk) for pk in OaiRecord.objects.filter(deleted=False).values_list("entry_id", flat=True)}
        result = self.sync(candidates)
        logger.info("oai.reconcile dry_run=%s %s", self.dry_run, result.as_dict())
        return result

    def _sync_chunk(self, chunk: list[str]) -> SyncResult:
        result = SyncResult()
        entries = {str(entry.pk): entry for entry in projector.load(chunk)}
        registered = projector.registered_entry_ids(entries.keys()) if entries else set()
        rows = {
            str(entry_id): (fingerprint, deleted)
            for entry_id, fingerprint, deleted in OaiRecord.objects.filter(entry_id__in=chunk).values_list(
                "entry_id", "fingerprint", "deleted"
            )
        }
        gone = []
        for entry_id in chunk:
            entry = entries.get(entry_id)
            row = rows.get(entry_id)
            if entry is None or not projector.is_exposed(entry, registered):
                if row is None:
                    result.skipped += 1
                elif row[1]:
                    result.unchanged += 1
                else:
                    gone.append(entry_id)
                continue

            projection = projector.project(entry)
            specs = projector.set_specs(entry)
            fingerprint = projector.fingerprint(projection, specs)
            if row is not None and row[0] == fingerprint and not row[1]:
                result.unchanged += 1
                continue
            if self.dry_run or self._upsert(entry_id, specs, fingerprint, projection):
                if row is None:
                    result.created += 1
                elif row[1]:
                    result.revived += 1
                else:
                    result.updated += 1
            else:
                # A concurrent sync wrote the same content first.
                result.unchanged += 1

        if gone:
            result.deleted += len(gone) if self.dry_run else self._tombstone(gone)
        return result

    @staticmethod
    def _upsert(entry_id: str, specs: list[str], fingerprint: str, projection: dict) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(_UPSERT, [entry_id, specs, fingerprint, json.dumps(projection, ensure_ascii=False)])
            return cursor.rowcount > 0

    @staticmethod
    def _tombstone(entry_ids: list[str]) -> int:
        """Keep the identifier and the last setSpecs (selective harvests of the set learn of the deletion)."""
        return OaiRecord.objects.filter(entry_id__in=entry_ids, deleted=False).update(
            deleted=True, projection=None, datestamp=RawSQL(DATESTAMP_SQL, ())
        )


def sync(entry_ids: Iterable, dry_run: bool = False) -> SyncResult:
    return OaiRecordSync(dry_run=dry_run).sync(entry_ids)


def reconcile(dry_run: bool = False) -> SyncResult:
    return OaiRecordSync(dry_run=dry_run).reconcile()


__all__ = ["BATCH_SIZE", "OaiRecordSync", "SyncResult", "reconcile", "sync"]

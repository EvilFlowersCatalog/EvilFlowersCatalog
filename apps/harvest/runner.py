"""One harvest run of one source: what the `harvest.run` task, the `harvest` command and the tests call.

Records are applied in batches of EVILFLOWERS_HARVEST_BATCH_SIZE per transaction, each record in its own
savepoint, so one bad record fails alone. The source's advisory lock is held for the whole run.

- **Unchanged records are free:** a record whose `raw_hash` equals the mirror row's, and whose entry still holds
  the link, is not applied again (unless `full`). Unmatched, ambiguous and failed rows are always re-matched:
  the catalog may have gained the entry since.
- **Dry run:** matching and planning only. No entry, category or cursor is written; `HarvestRecord`s are, with
  `dry_run=True`, after the source's previous dry-run rows are cleared.
- **Full run:** no `from`, no hash skip; when the reader lists the whole source, mirror rows not seen are
  treated as deleted at source.
- **`sync` sources:** changed records go through the three-way merge (`apps.harvest.sync`), deleted ones
  withdraw their entry, and entries of the target catalog with no remote record yet get a `harvest.push`.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.harvest.adapters import adapter_for
from apps.harvest.apply import Applier, Outcome, run_user, store_record
from apps.harvest.matching import MatchIndex, source_lock
from apps.harvest.models import HarvestRecord, HarvestSource
from apps.harvest.records import SourceRecord

logger = logging.getLogger(__name__)

Status = HarvestRecord.Status

#: Counter names, in the order a report lists them.
COUNTERS = (
    "seen",
    "created",
    "enriched",
    "matched",
    "unmatched",
    "ambiguous",
    "suggested",
    "skipped",
    "failed",
    "deleted",
    "unchanged",
)

LINKED = (Status.MATCHED, Status.CREATED, Status.ENRICHED)


class Run:
    def __init__(self, source: HarvestSource, *, dry_run: bool, full: bool, adapter=None, progress=None):
        self.source = source
        self.dry_run = dry_run
        self.full = full
        self.adapter = adapter or adapter_for(source, full=full)
        self.progress = progress
        self.counters = Counter({name: 0 for name in COUNTERS})
        self.seen: set[str] = set()
        self.user = run_user(source)
        self.sync = None

    def execute(self) -> dict:
        started = timezone.now()
        with source_lock(self.source):
            if self.dry_run:
                HarvestRecord.objects.filter(source=self.source, dry_run=True).delete()
            else:
                HarvestSource.objects.filter(pk=self.source.pk).update(last_run_at=started)
            self.index = MatchIndex(self.source.target_catalog, self.source.system_key)
            self.applier = Applier(self.source, self.index, user=self.user, dry_run=self.dry_run)
            if self.source.mode == HarvestSource.Mode.SYNC:
                from apps.harvest.sync import SyncEngine

                self.sync = SyncEngine(self.source, index=self.index, applier=self.applier, dry_run=self.dry_run)
            else:
                from apps.harvest.suggest import Suggester

                # #207: records no identifier places may get a suggested entry, never a link.
                self.applier.suggester = Suggester(self.source, dry_run=self.dry_run)

            batch: list[SourceRecord] = []
            for record in self.adapter.records():
                batch.append(record)
                if len(batch) >= settings.EVILFLOWERS_HARVEST_BATCH_SIZE:
                    self._batch(batch)
                    batch = []
            if batch:
                self._batch(batch)

            if self.full and getattr(self.adapter, "exhaustive", False):
                self._sweep_unseen()
            if self.sync is not None and not self.dry_run:
                self.counters["push_declared"] = self.sync.declare_unlinked()
        if self.applier.suggester is not None:
            self.applier.suggester.report()
        report = self.report(started)
        if not self.dry_run and getattr(self.adapter, "cursor", None):
            self.source.cursor = self.adapter.cursor
        self.source.last_report = report
        if not self.dry_run:
            self.source.last_run_at = started
        HarvestSource.objects.filter(pk=self.source.pk).update(
            cursor=self.source.cursor, last_report=report, last_run_at=self.source.last_run_at
        )
        logger.info("harvest.run source=%s %s", self.source.slug, " ".join(f"{k}={v}" for k, v in report.items()))
        return report

    def report(self, started) -> dict:
        report = {name: self.counters[name] for name in COUNTERS}
        if "push_declared" in self.counters:
            report["push_declared"] = self.counters["push_declared"]
        report.update(
            dry_run=self.dry_run,
            full=self.full,
            cursor=getattr(self.adapter, "cursor", None),
            started_at=started.isoformat(),
            finished_at=timezone.now().isoformat(),
        )
        return report

    # -- records -------------------------------------------------------------------------------------------

    def _batch(self, records: list[SourceRecord]) -> None:
        with transaction.atomic():
            ids = [self._resolve_id(record) for record in records]
            rows = {
                row.record_id: row
                for row in HarvestRecord.objects.filter(source=self.source, dry_run=False, record_id__in=ids)
            }
            for record in records:
                self.counters["seen"] += 1
                self.seen.add(record.record_id)
                try:
                    with transaction.atomic():
                        self._record(record, rows.get(record.record_id))
                except Exception as error:  # one record's bug must not end the run
                    logger.exception("harvest.record_failed source=%s record=%s", self.source.slug, record.record_id)
                    self.counters["failed"] += 1
                    self._store(record, Outcome(Status.FAILED, message=f"internal error: {error!r}"))
        if self.progress is not None:
            self.progress(dict(self.counters))

    def _resolve_id(self, record: SourceRecord) -> str:
        """A deleted OAI header carries the OAI identifier; the mirror row knows the record's own id."""
        if record.deleted:
            identifier = record.extra.get("oai_identifier")
            if identifier:
                row = (
                    HarvestRecord.objects.filter(
                        source=self.source, dry_run=False, source_record__extra__oai_identifier=identifier
                    )
                    .only("record_id")
                    .first()
                )
                if row is not None:
                    record.record_id = row.record_id
        return record.record_id

    def _record(self, record: SourceRecord, row: Optional[HarvestRecord]) -> None:
        if record.extra.get("error"):
            outcome = Outcome(Status.FAILED, message=record.extra["error"])
        elif self._unchanged(record, row):
            self.counters["unchanged"] += 1
            if self.dry_run:
                self._store(record, Outcome(row.status, row.entry, row.matched_on, "unchanged"))
            return
        elif self.sync is not None:
            outcome = self.sync.inbound(record, row)
        elif record.deleted:
            outcome = self.applier.remove(record.record_id)
        else:
            outcome = self.applier.apply(record)
        self.counters[outcome.status] += 1
        row = self._store(record, outcome)
        if self.dry_run:
            return
        from apps.harvest import suggest
        from apps.harvest.models import MatchSuggestion

        if outcome.suggestion is not None:
            suggest.store(row, outcome.suggestion)
        elif outcome.status in LINKED:
            # An identifier placed the record before anyone decided: the suggestion is moot.
            row.match_suggestions.filter(status=MatchSuggestion.Status.PENDING).update(
                status=MatchSuggestion.Status.SUPERSEDED, updated_at=timezone.now()
            )

    def _unchanged(self, record: SourceRecord, row: Optional[HarvestRecord]) -> bool:
        if self.full or record.deleted or row is None or row.deleted or row.entry_id is None:
            return False
        if row.status not in LINKED or row.raw_hash != record.raw_hash():
            return False
        # The link may have been removed by hand since: then the record is applied again.
        return self.index.system_of.get(str(row.entry_id)) == record.record_id

    def _store(self, record: SourceRecord, outcome: Outcome) -> HarvestRecord:
        return store_record(self.source, record, outcome, dry_run=self.dry_run)

    def _sweep_unseen(self) -> None:
        """A full harvest did not list these records: gone at source."""
        rows = HarvestRecord.objects.filter(source=self.source, dry_run=False, deleted=False).exclude(
            record_id__in=self.seen
        )
        for row in rows.iterator():
            with transaction.atomic():
                gone = SourceRecord.from_dict({**(row.source_record or {}), "record_id": row.record_id})
                gone.deleted = True
                if self.sync is not None:
                    outcome = self.sync.inbound(gone, row)
                else:
                    outcome = self.applier.remove(row.record_id)
                outcome.message = outcome.message or "not listed by a full harvest"
                self.counters[outcome.status] += 1
                self._store(gone, outcome)


def run_source(
    source: HarvestSource,
    *,
    dry_run: bool = False,
    full: bool = False,
    adapter=None,
    progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Harvest `source` once; the counters of the run (`COUNTERS` plus `dry_run`, `full`, `cursor`, times)."""
    return Run(source, dry_run=dry_run, full=full, adapter=adapter, progress=progress).execute()


__all__ = ["COUNTERS", "run_source", "run_user"]

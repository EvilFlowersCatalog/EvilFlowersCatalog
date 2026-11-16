"""Exact-identifier matching (IP-034 Q1) and the per-source advisory lock.

The index holds the target catalog's entries keyed by *normalised* identifiers: stored ISBNs may be hyphenated
or ISBN-10, so both sides go through `apps.harvest.records` normalisers. It is built once per run and kept up to
date as the run links and creates entries, so record N sees what record N-1 did.

Order (proposal "Matching"):

1. `identifiers[system_key] == record_id` → that entry (two entries holding it → ambiguous);
2. ISBN (with the record's further ISBNs), ISSN, ČNB, DOI in that order; the first identifier type with any hit
   decides: one entry → match, more → ambiguous; nothing at all → unmatched;
3. an entry found in 2. that already holds a *different* `system_key` value → ambiguous: it is another
   record's, and the database cannot index uniqueness of a configured key.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from django.db import connection

from apps.core.models import Entry
from apps.harvest.records import NORMALIZERS, SourceRecord

IDENTIFIER_ORDER = ("isbn", "issn", "ccnb", "doi")


@dataclass(frozen=True)
class Match:
    status: str  # matched · unmatched · ambiguous
    entry_id: Optional[str] = None
    matched_on: Optional[str] = None
    message: Optional[str] = None
    #: `ambiguous` on an identifier: the entries sharing it (#207 lets the decider choose among them).
    candidates: tuple = ()


class MatchIndex:
    def __init__(self, catalog, system_key: str):
        self.catalog = catalog
        self.system_key = system_key
        self.by_system: dict[str, set[str]] = defaultdict(set)
        self.by_identifier: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.system_of: dict[str, str] = {}
        for pk, identifiers in Entry.objects.filter(catalog=catalog).values_list("pk", "identifiers").iterator():
            self.add(str(pk), identifiers or {})

    def add(self, entry_id: str, identifiers: dict) -> None:
        for key, normalizer in NORMALIZERS.items():
            value = normalizer(identifiers.get(key))
            if value:
                self.by_identifier[(key, value)].add(entry_id)
        record_id = identifiers.get(self.system_key)
        if record_id:
            self.link(entry_id, record_id)

    def link(self, entry_id: str, record_id: str) -> None:
        previous = self.system_of.get(entry_id)
        if previous is not None:
            self.by_system[previous].discard(entry_id)
        self.system_of[entry_id] = record_id
        self.by_system[record_id].add(entry_id)

    def unlink(self, entry_id: str) -> None:
        previous = self.system_of.pop(entry_id, None)
        if previous is not None:
            self.by_system[previous].discard(entry_id)

    def holders(self, record_id: str) -> set[str]:
        return set(self.by_system.get(record_id) or ())

    def match(self, record: SourceRecord) -> Match:
        holders = self.holders(record.record_id)
        if len(holders) == 1:
            return Match("matched", next(iter(holders)), self.system_key)
        if len(holders) > 1:
            return Match(
                "ambiguous",
                message=f"{len(holders)} entries hold {self.system_key}={record.record_id}",
            )

        for key in IDENTIFIER_ORDER:
            values = [record.identifiers.get(key)] + (list(record.other_isbns) if key == "isbn" else [])
            candidates: set[str] = set()
            for value in values:
                normalised = NORMALIZERS[key](value)
                if normalised:
                    candidates |= self.by_identifier.get((key, normalised), set())
            if not candidates:
                continue
            if len(candidates) > 1:
                return Match(
                    "ambiguous",
                    matched_on=key,
                    message=f"{len(candidates)} entries share the {key}",
                    candidates=tuple(sorted(candidates)),
                )
            entry_id = next(iter(candidates))
            current = self.system_of.get(entry_id)
            if current and current != record.record_id:
                return Match(
                    "ambiguous",
                    matched_on=key,
                    message=f"the entry matched on {key} is already linked to {self.system_key}={current}",
                )
            return Match("matched", entry_id, key)
        return Match("unmatched")


class SourceBusy(Exception):
    """Another run of the same source holds its advisory lock."""


def _lock_key(source) -> int:
    digest = hashlib.sha256(f"harvest:{source.pk}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def source_lock(source):
    """Session-level PostgreSQL advisory lock per source: a run's batches span many transactions."""
    key = _lock_key(source)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise SourceBusy(f"Another run of harvest source '{source.slug}' is in progress.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


__all__ = ["IDENTIFIER_ORDER", "Match", "MatchIndex", "SourceBusy", "source_lock"]

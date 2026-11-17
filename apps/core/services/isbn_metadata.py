"""Bibliographic metadata by ISBN (#202): authors, publisher and year of records that lack them.

A queue of providers (`EVILFLOWERS_ISBN_METADATA_SERVICES`, drivers in `apps.core.services.isbn_providers`) is
asked in order. The first match does not end the lookup: each field takes the value of the first provider that
has it, and the queue goes on while a wanted field is still empty (an edited volume from Crossref without a year,
a Slovak book Open Library knows without a publisher). It stops as soon as every wanted field is filled, or when
the providers run out. `sources` says which provider gave which field.

Used at import, when the entry's `evilflowers_metadata_fetch` is on (`EntryService.populate`), and over existing
entries by `manage.py enrich_from_isbn`.

Only empty fields are filled: a publisher, a year or authors a librarian entered are never replaced, and authors
are added only to an entry that has none (never cleared). `EntryAuthor` has no role, so an edited volume without
authors gets its editors as authors; a provider that tells them apart (Crossref, the Slovak National Library)
keeps them apart in `IsbnMetadata.editors` until a role exists.

A provider that fails (network, quota, unreadable answer) is logged and the next one is asked; no failure reaches
the caller.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from django.conf import settings
from isbnlib.registry import bibformatters

from apps.core.services import isbn_providers
from apps.harvest.records import SourceAuthor, normalize_isbn

logger = logging.getLogger(__name__)

AUTHORS = "authors"
PUBLISHER = "publisher"
PUBLISHED_AT = "published_at"
FIELDS = (AUTHORS, PUBLISHER, PUBLISHED_AT)

#: Record fields merged by the queue, each from the first provider that has it.
_MERGED = ("authors", "editors", "publisher", "year", "title", "language", "doi")
#: What a wanted entry field needs from the record: authors are satisfied by editors as well.
_NEEDS = {AUTHORS: ("authors", "editors"), PUBLISHER: ("publisher",), PUBLISHED_AT: ("year",)}


@dataclass
class IsbnMetadata:
    isbn: str
    authors: List[SourceAuthor] = field(default_factory=list)
    editors: List[SourceAuthor] = field(default_factory=list)
    publisher: Optional[str] = None
    year: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    doi: Optional[str] = None
    #: `{field: provider}` of every field that has a value.
    sources: Dict[str, str] = field(default_factory=dict)
    #: Providers asked, in order, with what each answered: `found`, `none`, `failed`, `skipped`.
    asked: Dict[str, str] = field(default_factory=dict)

    @property
    def service(self) -> str:
        """The providers that contributed, in queue order (`crossref+snk`)."""
        return "+".join(dict.fromkeys(self.sources.values())) or "-"

    @property
    def people(self) -> List[SourceAuthor]:
        """Who goes into `EntryAuthor`: the authors, else (an edited volume) the editors."""
        return self.authors or self.editors

    def has(self, wanted: str) -> bool:
        return any(getattr(self, name) for name in _NEEDS[wanted])

    def merge(self, record: isbn_providers.Record) -> List[str]:
        """Take the record's values for fields still empty. Returns the fields it filled."""
        filled = []
        for name in _MERGED:
            value = getattr(record, name)
            if value and not getattr(self, name):
                setattr(self, name, value)
                self.sources[name] = record.provider
                filled.append(name)
        return filled

    @property
    def bibtex(self) -> Optional[str]:
        if not (self.title or self.people):
            return None
        canonical = {
            "ISBN-13": self.isbn,
            "Title": self.title or "",
            "Authors": [" ".join(part for part in (person.name, person.surname) if part) for person in self.people],
            "Publisher": self.publisher or "",
            "Year": self.year or "",
            "Language": self.language or "",
        }
        try:
            return bibformatters["bibtex"](canonical)
        except Exception:  # noqa: BLE001 - a malformed record must not break the import
            logger.warning("isbn_metadata.bibtex_failed isbn=%s", self.isbn)
            return None


def lookup(
    value: Optional[str], services: Optional[List[str]] = None, wanted: Optional[Iterable[str]] = None
) -> Optional[IsbnMetadata]:
    """Ask the providers in order until every `wanted` field (default: authors, publisher, year) has a value.

    The ISBN may be stored as `urn:isbn:…`, hyphenated or ISBN-10. None when the ISBN is invalid or no provider
    knew it.
    """
    isbn = normalize_isbn(value)
    if not isbn:
        return None
    wanted = set(FIELDS if wanted is None else wanted)
    metadata = IsbnMetadata(isbn=isbn)
    for name in services if services is not None else settings.EVILFLOWERS_ISBN_METADATA_SERVICES:
        if wanted and all(metadata.has(item) for item in wanted):
            break
        provider = isbn_providers.PROVIDERS.get(name)
        if provider is None:
            logger.warning("isbn_metadata.unknown_provider provider=%s", name)
            metadata.asked[name] = "skipped"
            continue
        try:
            record = provider(isbn)
        except isbn_providers.ProviderError as error:
            logger.warning("isbn_metadata.lookup_failed isbn=%s provider=%s error=%s", isbn, name, error)
            metadata.asked[name] = "failed"
            continue
        except Exception:  # noqa: BLE001 - a driver bug must not break an import
            logger.exception("isbn_metadata.provider_crashed isbn=%s provider=%s", isbn, name)
            metadata.asked[name] = "failed"
            continue
        if record is None:
            metadata.asked[name] = "none"
            continue
        metadata.merge(record)
        metadata.asked[name] = "found"
    return metadata if metadata.sources else None


def missing(entry) -> set:
    """Which of authors, publisher and year the entry lacks."""
    gaps = set()
    if not entry.pk or not entry.entry_authors.exists():
        gaps.add(AUTHORS)
    if not (entry.publisher or "").strip():
        gaps.add(PUBLISHER)
    if entry.published_at is None:
        gaps.add(PUBLISHED_AT)
    return gaps


def changes(entry, metadata: IsbnMetadata) -> dict:
    """`{field: value}` the metadata would fill: only fields the entry lacks and the record has."""
    gaps = missing(entry)
    found = {}
    if AUTHORS in gaps and metadata.people:
        found[AUTHORS] = metadata.people
    if PUBLISHER in gaps and metadata.publisher:
        found[PUBLISHER] = metadata.publisher
    if PUBLISHED_AT in gaps and metadata.year:
        found[PUBLISHED_AT] = metadata.year
    return found


def fill_fields(entry, metadata: IsbnMetadata) -> dict:
    """Publisher and year onto the (unsaved) entry where empty. Returns what was set."""
    from partial_date import PartialDate

    found = {key: value for key, value in changes(entry, metadata).items() if key != AUTHORS}
    if PUBLISHER in found:
        entry.publisher = found[PUBLISHER]
    if PUBLISHED_AT in found:
        entry.published_at = PartialDate(found[PUBLISHED_AT])
    return found


def fill_authors(entry, metadata: IsbnMetadata) -> List[SourceAuthor]:
    """Authors (else editors) of the record onto a saved entry that has none; never removes a link."""
    from apps.core.models import Author, EntryAuthor

    people = metadata.people
    if not people or entry.entry_authors.exists():
        return []
    for position, parsed in enumerate(people):
        author = Author.objects.filter(catalog=entry.catalog, surname=parsed.surname, name=parsed.name).first()
        author = author or Author.objects.create(catalog=entry.catalog, surname=parsed.surname, name=parsed.name)
        EntryAuthor.objects.create(entry=entry, author=author, position=position)
    return list(people)


def enrich(entry, metadata: Optional[IsbnMetadata] = None, *, dry_run: bool = False) -> dict:
    """Fill what an existing entry lacks from its ISBN. Returns the changes (also on a dry run)."""
    from django.db import transaction

    if metadata is None:
        gaps = missing(entry)
        if not gaps:
            return {}
        metadata = lookup((entry.identifiers or {}).get("isbn"), wanted=gaps)
    if metadata is None:
        return {}
    found = changes(entry, metadata)
    if dry_run or not found:
        return found
    with transaction.atomic():
        fill_fields(entry, metadata)
        update_fields = [key for key in (PUBLISHER, PUBLISHED_AT) if key in found]
        if entry.citation is None and metadata.bibtex:
            entry.citation = metadata.bibtex
            update_fields.append("citation")
        if update_fields:
            entry.save(update_fields=[*update_fields, "updated_at"])
        fill_authors(entry, metadata)
    return found


def describe(found: dict, metadata: Optional[IsbnMetadata] = None) -> str:
    parts = []
    if AUTHORS in found:
        label = "editors" if metadata is not None and not metadata.authors else "authors"
        parts.append(f"{label}=" + "; ".join(f"{a.surname}, {a.name}".strip(", ") for a in found[AUTHORS]))
    if PUBLISHER in found:
        parts.append(f"publisher={found[PUBLISHER]}")
    if PUBLISHED_AT in found:
        parts.append(f"year={found[PUBLISHED_AT]}")
    if metadata is not None:
        parts.append(f"from {metadata.service}")
    return " ".join(parts)


__all__ = ["IsbnMetadata", "changes", "describe", "enrich", "fill_authors", "fill_fields", "lookup", "missing"]

"""Bibliographic metadata by ISBN (#202): authors, publisher and year of records that lack them.

Looked up with isbnlib against free services only (`EVILFLOWERS_ISBN_METADATA_SERVICES`, tried in order). Used at
import, when the entry's `evilflowers_metadata_fetch` is on (`EntryService.populate`), and over existing entries by
`manage.py enrich_from_isbn`.

Only empty fields are filled: a publisher, a year or authors a librarian entered are never replaced, and authors
are added only to an entry that has none (never cleared). isbnlib does not tell authors from editors and
`EntryAuthor` has no role, so editors of an edited volume become its authors.

A service that fails (network, quota, unknown ISBN) is logged and the next one is asked; no failure reaches the
caller.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import isbnlib
from django.conf import settings
from isbnlib.registry import bibformatters

from apps.harvest.records import SourceAuthor, normalize_isbn

logger = logging.getLogger(__name__)

AUTHORS = "authors"
PUBLISHER = "publisher"
PUBLISHED_AT = "published_at"
FIELDS = (AUTHORS, PUBLISHER, PUBLISHED_AT)

_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


@dataclass
class IsbnMetadata:
    isbn: str
    service: str
    authors: List[SourceAuthor] = field(default_factory=list)
    publisher: Optional[str] = None
    year: Optional[str] = None
    #: isbnlib's canonical record, for the BibTeX citation.
    raw: dict = field(default_factory=dict)

    @property
    def bibtex(self) -> Optional[str]:
        try:
            return bibformatters["bibtex"](self.raw) if self.raw else None
        except Exception:  # noqa: BLE001 - a malformed record must not break the import
            logger.warning("isbn_metadata.bibtex_failed isbn=%s service=%s", self.isbn, self.service)
            return None


def _parse(isbn: str, service: str, raw: dict) -> Optional[IsbnMetadata]:
    if not raw:
        return None
    authors = [SourceAuthor.parse(name) for name in raw.get("Authors") or [] if (name or "").strip()]
    authors = [author for author in authors if author.surname]
    year = _YEAR.search(str(raw.get("Year") or ""))
    publisher = " ".join(str(raw.get("Publisher") or "").split())[:255] or None
    return IsbnMetadata(
        isbn=isbn,
        service=service,
        authors=authors,
        publisher=publisher,
        year=year.group(1) if year else None,
        raw=raw,
    )


def lookup(value: Optional[str], services: Optional[List[str]] = None) -> Optional[IsbnMetadata]:
    """The first service's record for the ISBN (stored as `urn:isbn:…`, hyphenated or ISBN-10), or None."""
    isbn = normalize_isbn(value)
    if not isbn:
        return None
    for service in services if services is not None else settings.EVILFLOWERS_ISBN_METADATA_SERVICES:
        try:
            raw = isbnlib.meta(isbn, service=service)
        except Exception as error:  # noqa: BLE001 - isbnlib raises its own and requests' errors alike
            logger.warning("isbn_metadata.lookup_failed isbn=%s service=%s error=%s", isbn, service, error)
            continue
        metadata = _parse(isbn, service, raw)
        if metadata is not None:
            return metadata
    return None


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
    if AUTHORS in gaps and metadata.authors:
        found[AUTHORS] = metadata.authors
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
    """Authors of the record onto a saved entry that has none; never removes a link. Returns those added."""
    from apps.core.models import Author, EntryAuthor

    if not metadata.authors or entry.entry_authors.exists():
        return []
    for position, parsed in enumerate(metadata.authors):
        author = Author.objects.filter(catalog=entry.catalog, surname=parsed.surname, name=parsed.name).first()
        author = author or Author.objects.create(catalog=entry.catalog, surname=parsed.surname, name=parsed.name)
        EntryAuthor.objects.create(entry=entry, author=author, position=position)
    return list(metadata.authors)


def enrich(entry, metadata: Optional[IsbnMetadata] = None, *, dry_run: bool = False) -> dict:
    """Fill what an existing entry lacks from its ISBN. Returns the changes (also on a dry run)."""
    from django.db import transaction

    if metadata is None:
        metadata = lookup((entry.identifiers or {}).get("isbn"))
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


def describe(found: dict) -> str:
    parts = []
    if AUTHORS in found:
        parts.append(
            "authors=" + "; ".join(f"{author.surname}, {author.name}".strip(", ") for author in found[AUTHORS])
        )
    if PUBLISHER in found:
        parts.append(f"publisher={found[PUBLISHER]}")
    if PUBLISHED_AT in found:
        parts.append(f"year={found[PUBLISHED_AT]}")
    return " ".join(parts)


__all__ = ["IsbnMetadata", "changes", "describe", "enrich", "fill_authors", "fill_fields", "lookup", "missing"]

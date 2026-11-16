"""Both directions of the entry ↔ `SourceRecord` mapping the two-way sync compares (IP-034 Part B).

`apps.harvest.apply` maps a record onto an entry; this module is its inverse (`project`) and the canonical
field values both sides are reduced to (`from_entry`, `from_record`), so equal content compares equal: the
full title, "Surname, Name" authors in order, sorted subject terms split into UDC and the rest, normalised
identifiers, the date string and an ISO 639-1 language code. A field that were lossy one way would look changed
after every sync. `Applier.apply_values` writes canonical values back onto an entry.

Not synced: series (`Entry` has no column), links and the licence (see `apps.harvest.apply`).
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from apps.core.models import Entry, EntryAuthor
from apps.harvest.adapters.marc import language_code
from apps.harvest.records import NORMALIZERS, UDC_SCHEME, SourceAuthor, SourceRecord, SourceSubject

#: Canonical fields both sides edit, merged one by one (Q10).
SYNC_FIELDS = (
    "title",
    "authors",
    "summary",
    "publisher",
    "published",
    "language",
    "edition",
    "page_count",
    "isbn",
    "issn",
    "ccnb",
    "doi",
    "udc",
    "subjects",
)
IDENTIFIER_FIELDS = ("isbn", "issn", "ccnb", "doi")

_TITLE_MAX = 255


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


def _author(surname: Optional[str], name: Optional[str]) -> Optional[str]:
    surname, name = _text(surname) or "", _text(name) or ""
    if not surname and not name:
        return None
    return f"{surname}, {name}" if surname and name else (surname or name)


def _terms(values) -> Optional[list[str]]:
    terms = sorted({term for term in (_text(value) for value in values) if term})
    return terms or None


def _language(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return language_code(code) or code.lower()


def _published(value: Any) -> Optional[str]:
    return _text(str(value)) if value not in (None, "") else None


def from_entry(entry: Entry) -> dict:
    identifiers = entry.identifiers or {}
    authors = [
        _author(link.author.surname, link.author.name)
        for link in EntryAuthor.objects.filter(entry=entry).select_related("author").order_by("position", "pk")
    ]
    categories = list(entry.categories.values_list("term", "scheme"))
    values = {
        "title": (_text(entry.title) or "")[:_TITLE_MAX] or None,
        "authors": [author for author in authors if author] or None,
        "summary": _text(entry.summary),
        "publisher": _text(entry.publisher),
        "published": _published(entry.published_at),
        "language": (entry.language.alpha2 or entry.language.alpha3).lower() if entry.language_id else None,
        "edition": _text(entry.edition),
        "page_count": entry.page_count or None,
        "udc": _terms(term for term, scheme in categories if scheme == UDC_SCHEME),
        "subjects": _terms(term for term, scheme in categories if scheme != UDC_SCHEME),
    }
    for key in IDENTIFIER_FIELDS:
        # A value that does not normalise is kept as typed: read as empty, it would clear the other side's.
        values[key] = NORMALIZERS[key](identifiers.get(key)) or _text(identifiers.get(key))
    return values


def from_record(record: SourceRecord) -> dict:
    values = {
        "title": (_text(record.full_title) or "")[:_TITLE_MAX] or None,
        "authors": [a for a in (_author(item.surname, item.name) for item in record.authors) if a] or None,
        "summary": _text(record.summary),
        "publisher": _text(record.publisher),
        "published": _published(record.published),
        "language": _language(record.language),
        "edition": _text(record.edition),
        "page_count": record.page_count or None,
        "udc": _terms(subject.term for subject in record.subjects if subject.scheme == UDC_SCHEME),
        "subjects": _terms(subject.term for subject in record.subjects if subject.scheme != UDC_SCHEME),
    }
    for key in IDENTIFIER_FIELDS:
        values[key] = NORMALIZERS[key](record.identifiers.get(key)) or _text(record.identifiers.get(key))
    return values


def to_record(values: dict, *, record_id: str, scheme: str, template: Optional[SourceRecord] = None) -> SourceRecord:
    """A `SourceRecord` holding canonical `values`; what `template` (the remote's current record) has beyond the
    synced fields (series, links, licence, `extra`) is kept, so a push never drops them."""
    record = copy.deepcopy(template) if template is not None else SourceRecord(record_id=record_id)
    record.record_id = record_id
    record.title = values.get("title") or ""
    record.subtitle = None
    record.authors = [SourceAuthor.parse(value) for value in values.get("authors") or []]
    record.summary = values.get("summary")
    record.publisher = values.get("publisher")
    record.published = values.get("published")
    record.language = values.get("language")
    record.edition = values.get("edition")
    record.page_count = values.get("page_count")
    record.identifiers = {key: values[key] for key in IDENTIFIER_FIELDS if values.get(key)}
    known = {subject.term: subject for subject in (template.subjects if template is not None else [])}
    record.subjects = [SourceSubject(term=term, scheme=UDC_SCHEME) for term in values.get("udc") or []] + [
        SourceSubject(term=term, scheme=(known[term].scheme if term in known else scheme), label=None)
        for term in values.get("subjects") or []
    ]
    return record


def project(entry: Entry, source) -> SourceRecord:
    """The entry as a record of `source` (the local side of the sync, and what a push sends)."""
    record = to_record(
        from_entry(entry),
        record_id=(entry.identifiers or {}).get(source.system_key) or "",
        scheme=source.slug,
    )
    record.version = entry.updated_at.isoformat() if entry.updated_at else None
    record.extra = {"entry_id": str(entry.pk)}
    return record


__all__ = ["IDENTIFIER_FIELDS", "SYNC_FIELDS", "from_entry", "from_record", "project", "to_record"]

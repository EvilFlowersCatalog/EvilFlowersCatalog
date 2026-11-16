"""Unqualified Dublin Core (`oai_dc`) → `SourceRecord`.

`oai_dc` carries no typed identifiers, so ISBN, ISSN and DOI are recognised by their prefixes and checksums.
"""

from __future__ import annotations

import re
from typing import Optional

from apps.harvest.adapters.base import local, text
from apps.harvest.adapters.marc import clean, language_code, year
from apps.harvest.records import (
    SourceAuthor,
    SourceRecord,
    SourceSubject,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
)


def _values(element, name: str) -> list[str]:
    return [value for value in (text(item) for item in element.iter() if local(item) == name) if value]


def _identifier(value: str, identifiers: dict, isbns: list, links: list) -> None:
    lowered = value.lower()
    if "doi" in lowered or lowered.startswith("10."):
        doi = normalize_doi(re.sub(r"^(info:doi/|urn:doi:)", "", value, flags=re.IGNORECASE))
        if doi:
            identifiers.setdefault("doi", doi)
            return
    if "isbn" in lowered or re.fullmatch(r"[\d\- Xx]{10,17}", value):
        isbn = normalize_isbn(re.sub(r"^(urn:isbn:|isbn:?\s*)", "", value, flags=re.IGNORECASE))
        if isbn:
            if isbn not in isbns:
                isbns.append(isbn)
            return
    if "issn" in lowered:
        issn = normalize_issn(re.sub(r"^(urn:issn:|issn:?\s*)", "", value, flags=re.IGNORECASE))
        if issn:
            identifiers.setdefault("issn", issn)
            return
    if lowered.startswith(("http://", "https://")) and value not in links:
        links.append(value)


def to_source_record(element, *, record_id: str, subject_scheme: str) -> SourceRecord:
    """Map an `<oai_dc:dc>` element; `record_id` is the OAI identifier (DC has no record number)."""
    identifiers: dict[str, str] = {}
    isbns: list[str] = []
    links: list[str] = []
    for value in _values(element, "identifier"):
        _identifier(value, identifiers, isbns, links)
    if isbns:
        identifiers["isbn"] = isbns[0]

    titles = _values(element, "title")
    title, subtitle = (titles[0] if titles else ""), None
    if " : " in title:
        title, subtitle = (part.strip() for part in title.split(" : ", 1))

    language: Optional[str] = None
    for value in _values(element, "language"):
        language = language_code(value)
        if language:
            break

    summaries = [clean(value) for value in _values(element, "description")]
    rights = _values(element, "rights")
    licence_url = next((value for value in rights if value.lower().startswith(("http://", "https://"))), None)

    return SourceRecord(
        record_id=record_id,
        title=clean(title) or "",
        subtitle=clean(subtitle),
        authors=[SourceAuthor.parse(value) for value in _values(element, "creator")],
        identifiers=identifiers,
        other_isbns=isbns[1:],
        language=language,
        publisher=clean(next(iter(_values(element, "publisher")), None)),
        published=next((year(value) for value in _values(element, "date") if year(value)), None),
        summary="\n\n".join(value for value in summaries if value) or None,
        subjects=[
            SourceSubject(term=term[:255], scheme=subject_scheme)
            for term in dict.fromkeys(clean(value) for value in _values(element, "subject"))
            if term
        ],
        licence_url=licence_url,
        licence_name=next((value for value in rights if value != licence_url), None),
        links=links,
    )


__all__ = ["to_source_record"]

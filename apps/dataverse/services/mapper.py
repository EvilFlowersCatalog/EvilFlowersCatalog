"""
Dataverse dataset → EvilFlowers metadata mapper.

The legacy `DataversePrepublishIngest.post` had seven nested closures
(`flatten_text`, `first_text`, `parse_partial_date`, `resolve_language`,
`build_content`, `build_citation`, `extract_metadata`). They are pulled
out here as module-level functions so each is independently testable
and the orchestration in `services/sync.py` stays small.

Each helper is a pure function over the Dataverse payload shape; the
only side-channel is `resolve_language`, which does a DB lookup against
the `Language` table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from partial_date import PartialDate

import pycountry

from apps.core.models import Acquisition, Language
from apps.harvest.records import (
    UDC_SCHEME,
    SourceAuthor,
    SourceRecord,
    SourceSubject,
    isbn_valid,
    normalize_ccnb,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
)


def map_content_type_to_mime(content_type: Optional[str]) -> str:
    """Translate a Dataverse `contentType` to an `Acquisition.AcquisitionMIME`.

    Unknown / missing types default to PDF — matches legacy behaviour.
    """
    ct = (content_type or "").lower()
    if "pdf" in ct or ct == "application/pdf":
        return Acquisition.AcquisitionMIME.PDF
    if "epub" in ct or ct == "application/epub+zip":
        return Acquisition.AcquisitionMIME.EPUB
    if "mobi" in ct or ct == "application/x-mobipocket-ebook":
        return Acquisition.AcquisitionMIME.MOBI
    if "webpub" in ct or ct == "application/webpub+zip":
        return Acquisition.AcquisitionMIME.READIUM_PACKAGE
    return Acquisition.AcquisitionMIME.PDF


def flatten_text(value: Any) -> List[str]:
    """Flatten a Dataverse metadata `value` into a list of strings.

    Dataverse fields can be strings, lists of strings, lists of dicts
    (with `value`/`displayValue`/etc.), or composite dicts with
    `firstName`/`lastName`. This consolidates all shapes into a flat
    list of cleaned strings.
    """
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(flatten_text(item))
        return out
    if isinstance(value, dict):
        first = value.get("firstName") or value.get("givenName") or ""
        last = value.get("lastName") or value.get("familyName") or ""
        if first or last:
            return [" ".join(part for part in [first, last] if part).strip()]
        for key in (
            "value",
            "displayValue",
            "authorName",
            "name",
            "keywordValue",
            "subject",
            "term",
            "title",
        ):
            if key in value:
                return flatten_text(value.get(key))
    return []


def first_text(*values: Any) -> Optional[str]:
    """Return the first non-empty flattened string from any of `values`."""
    for value in values:
        flat = flatten_text(value)
        if flat:
            return flat[0]
    return None


def parse_partial_date(value: Any) -> Optional[PartialDate]:
    """Parse a Dataverse-style date string into a `PartialDate`.

    Accepts `YYYY-MM-DD`, `YYYY-MM`, `YYYY`. Returns None for anything
    else (matches legacy lenient behaviour — dataverse fields are
    notoriously inconsistent).
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None

    # `PartialDate` takes a date and a precision (or the string itself); it never had year/month/day keywords,
    # so the old constructor call failed for every dated dataset.
    for fmt, precision in (
        ("%Y-%m-%d", PartialDate.DAY),
        ("%Y-%m", PartialDate.MONTH),
        ("%Y", PartialDate.YEAR),
    ):
        try:
            return PartialDate(datetime.strptime(cleaned, fmt).date(), precision)
        except ValueError:
            continue
    return None


def resolve_language(value: Any) -> Optional[Language]:
    """Resolve a Dataverse `language` field to a `Language` row.

    Tries alpha-2, alpha-3, then name (case-insensitive). DB lookup.
    """
    for candidate in flatten_text(value):
        normalized = candidate.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        language = None
        if len(lowered) == 2:
            language = Language.objects.filter(alpha2=lowered).first()
        elif len(lowered) == 3:
            language = Language.objects.filter(alpha3=lowered).first()
        if language is None:
            language = Language.objects.filter(name__iexact=normalized).first()
        if language:
            return language
    return None


def _citation_fields(dataset_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata_blocks = dataset_data.get("metadataBlocks") or {}
    citation_block = metadata_blocks.get("citation") or {}
    return citation_block.get("fields") or []


def _citation_field_value(dataset_data: Dict[str, Any], type_name: str) -> Any:
    for field in _citation_fields(dataset_data):
        if field.get("typeName") == type_name:
            return field.get("value")
    return None


def _extract_authors(dataset_data: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull author entries out of either `metadataBlocks.citation.author`
    or the legacy top-level `authors` field. Returns a list of
    `{"name": ..., "surname": ...}` dicts.
    """
    authors_list: List[Any] = []
    for field in _citation_fields(dataset_data):
        if field.get("typeName") == "author":
            authors_list = field.get("value", []) or []
            break
    if not authors_list:
        authors_list = dataset_data.get("authors") or []

    authors: List[Dict[str, str]] = []
    for author_data in authors_list:
        if not isinstance(author_data, dict):
            continue
        author_name = author_data.get("authorName") or author_data.get("name")
        first = author_data.get("firstName") or author_data.get("givenName")
        last = author_data.get("lastName") or author_data.get("familyName")

        if first or last:
            authors.append({"name": first or "", "surname": last or ""})
            continue

        if isinstance(author_name, str):
            parts = author_name.strip().split(None, 1)
            if len(parts) >= 2:
                authors.append({"name": parts[0], "surname": " ".join(parts[1:])})
            elif parts:
                authors.append({"name": parts[0], "surname": ""})
        elif isinstance(author_name, dict):
            first = author_name.get("firstName") or author_name.get("givenName") or ""
            last = author_name.get("lastName") or author_name.get("familyName") or ""
            if first or last:
                authors.append({"name": first, "surname": last})

    return authors


def _build_content(metadata: Dict[str, Any], files: List[Dict[str, Any]]) -> Optional[str]:
    lines: List[str] = []
    if metadata.get("summary"):
        lines.append(metadata["summary"])
    if metadata.get("authors"):
        rendered = ", ".join(
            " ".join(part for part in [author["name"], author["surname"]] if part).strip()
            for author in metadata["authors"]
            if author.get("name") or author.get("surname")
        )
        if rendered:
            lines.append(f"Authors: {rendered}")
    if metadata.get("publisher"):
        lines.append(f"Publisher: {metadata['publisher']}")
    if metadata.get("language"):
        lines.append(f"Language: {metadata['language'].name}")
    if metadata.get("doi"):
        lines.append(f"Persistent ID: {metadata['doi']}")

    file_names = [
        (item.get("dataFile") or {}).get("filename") for item in files if (item.get("dataFile") or {}).get("filename")
    ]
    if file_names:
        lines.append("Files: " + ", ".join(file_names))

    return "\n\n".join(line for line in lines if line) or None


def _build_citation(metadata: Dict[str, Any]) -> Optional[str]:
    author_names = [
        " ".join(part for part in [author["surname"], author["name"]] if part).strip(", ")
        for author in metadata.get("authors") or []
        if author.get("name") or author.get("surname")
    ]
    parts: List[str] = []
    if author_names:
        parts.append("; ".join(author_names))
    if metadata.get("title"):
        parts.append(metadata["title"])
    if metadata.get("publisher"):
        parts.append(metadata["publisher"])
    if metadata.get("published_at"):
        parts.append(str(metadata["published_at"]))
    if metadata.get("doi"):
        parts.append(metadata["doi"])
    return ". ".join(part for part in parts if part) or None


def extract_metadata(
    dataset_data: Dict[str, Any],
    files: List[Dict[str, Any]],
    *,
    title_override: Optional[str] = None,
    global_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the EvilFlowers-side metadata dict from a Dataverse dataset.

    `title_override` is the workflow payload's `title`; if absent, we
    derive it from the dataset itself. `global_id` only feeds the
    fallback title when nothing else is available.

    Returns a dict with keys: title, summary, content, authors (list of
    {name, surname}), language (Language|None), publisher, published_at
    (PartialDate|None), doi, citation.
    """
    metadata: Dict[str, Any] = {
        "title": title_override or "",
        "summary": None,
        "content": None,
        "authors": _extract_authors(dataset_data),
        "language": None,
        "publisher": None,
        "published_at": None,
        "doi": None,
        "citation": None,
    }

    if not metadata["title"]:
        metadata["title"] = (
            dataset_data.get("title")
            or dataset_data.get("displayName")
            or (f"Dataverse Dataset {global_id}" if global_id else "")
        )

    metadata["summary"] = first_text(
        dataset_data.get("description"),
        dataset_data.get("descriptionText"),
        _citation_field_value(dataset_data, "dsDescription"),
    )

    metadata["publisher"] = first_text(
        dataset_data.get("publisher"),
        dataset_data.get("producer"),
        _citation_field_value(dataset_data, "publisher"),
        _citation_field_value(dataset_data, "producerName"),
    )

    pub_date_value = (
        dataset_data.get("publicationDate")
        or dataset_data.get("datePublished")
        or first_text(_citation_field_value(dataset_data, "productionDate"))
        or first_text(_citation_field_value(dataset_data, "distributionDate"))
    )
    metadata["published_at"] = parse_partial_date(pub_date_value)

    metadata["doi"] = first_text(
        dataset_data.get("persistentId"),
        dataset_data.get("doi"),
    )
    if metadata["doi"] and not metadata["doi"].startswith("doi:"):
        if metadata["doi"].startswith("10."):
            metadata["doi"] = f"doi:{metadata['doi']}"

    metadata["language"] = resolve_language(
        _citation_field_value(dataset_data, "language") or dataset_data.get("language")
    )

    # IP-034: the native API's real shapes (compound subfields as `{"typeName", "value"}` dicts, `distributor`,
    # the `elvira_book` block) fill what the legacy lookups above miss.
    if dataset_data.get("metadataBlocks"):
        record = dataset_to_source_record(dataset_data, pid=global_id)
        if not title_override and record.title and not (dataset_data.get("title") or dataset_data.get("displayName")):
            metadata["title"] = record.title
        if not metadata["authors"]:
            metadata["authors"] = [{"name": a.name, "surname": a.surname} for a in record.authors]
        metadata["summary"] = metadata["summary"] or record.summary
        metadata["publisher"] = metadata["publisher"] or record.publisher
        metadata["published_at"] = metadata["published_at"] or parse_partial_date(record.published)
        metadata["record"] = record

    metadata["content"] = _build_content(metadata, files)
    metadata["citation"] = _build_citation(metadata)

    # #177: the dataset version's licence.
    from apps.content.rights import from_dataverse

    metadata["rights"] = from_dataverse(dataset_data)

    return metadata


# =============================================================================================================
# IP-034: a dataset version <-> `SourceRecord`, and the field payloads Elvira writes back.
#
# Native API field shapes (Dataverse 6.x): a primitive field is `{"typeName", "value": str | [str]}`, a
# controlled vocabulary one `{"value": [str]}`, a compound one `{"value": [{subfield: {"typeName", "value"}}]}`.
# =============================================================================================================

ELVIRA_BLOCK = "elvira_book"
DATASET_TYPE_BOOK = "book"

#: typeName -> (block, multiple, typeClass, compound subfields). Used to build create payloads.
FIELD_SPECS: Dict[str, tuple] = {
    "title": ("citation", False, "primitive", ()),
    "author": ("citation", True, "compound", ("authorName",)),
    "datasetContact": ("citation", True, "compound", ("datasetContactName", "datasetContactEmail")),
    "dsDescription": ("citation", True, "compound", ("dsDescriptionValue",)),
    "subject": ("citation", True, "controlledVocabulary", ()),
    "keyword": ("citation", True, "compound", ("keywordValue",)),
    "otherId": ("citation", True, "compound", ("otherIdAgency", "otherIdValue")),
    "distributor": ("citation", True, "compound", ("distributorName",)),
    "distributionDate": ("citation", False, "primitive", ()),
    "language": ("citation", True, "controlledVocabulary", ()),
    "relatedMaterial": ("citation", True, "primitive", ()),
    "isbn": (ELVIRA_BLOCK, True, "primitive", ()),
    "edition": (ELVIRA_BLOCK, False, "primitive", ()),
    "seriesTitle": (ELVIRA_BLOCK, False, "primitive", ()),
    "seriesNumber": (ELVIRA_BLOCK, False, "primitive", ()),
    "pageCount": (ELVIRA_BLOCK, False, "primitive", ()),
    "udc": (ELVIRA_BLOCK, True, "primitive", ()),
    "elviraEntryId": (ELVIRA_BLOCK, False, "primitive", ()),
    "elviraUrl": (ELVIRA_BLOCK, False, "primitive", ()),
    "lending": (ELVIRA_BLOCK, False, "primitive", ()),
}


def block_values(version: Dict[str, Any], block: str) -> Dict[str, Any]:
    """`{typeName: value}` of one metadata block of a dataset version."""
    fields = ((version.get("metadataBlocks") or {}).get(block) or {}).get("fields") or []
    return {field.get("typeName"): field.get("value") for field in fields if field.get("typeName")}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _sub(item: Any, name: str) -> Optional[str]:
    """A compound value's subfield (`{name: {"value": …}}`, or a plain string in hand-written JSON)."""
    if not isinstance(item, dict):
        return None
    return _text(item.get(name))


def _texts(value: Any) -> List[str]:
    return [text for text in (_text(item) for item in _as_list(value)) if text]


#: `otherId` agencies read as the Czech/Slovak national bibliography number.
CCNB_AGENCIES = ("CCNB", "ČNB", "CNB")


def language_code_of(value: Optional[str]) -> Optional[str]:
    """ISO 639-1 (else 639-3) of a language name or code ("Slovak", "slk", "sk"); None when unknown."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        language = pycountry.languages.lookup(value)
    except LookupError:
        return None
    return getattr(language, "alpha_2", None) or language.alpha_3


def language_name(code: Optional[str]) -> Optional[str]:
    """The English name Dataverse's `language` vocabulary uses ("Slovak"), from an ISO code."""
    if not code:
        return None
    try:
        return pycountry.languages.lookup(code).name
    except LookupError:
        return None


def dataset_type(dataset: Dict[str, Any]) -> Optional[str]:
    """The dataset type (6.4+), from the dataset or its version; None when the server predates types."""
    for holder in (dataset, dataset.get("latestVersion") or {}):
        value = holder.get("datasetType")
        if isinstance(value, dict):
            value = value.get("name")
        if value:
            return str(value)
    return None


def dataset_to_source_record(
    version: Dict[str, Any], *, pid: Optional[str] = None, dataset_id: Optional[str] = None
) -> SourceRecord:
    """A dataset version (`/versions/:latest-published` data) as a `SourceRecord`.

    `extra` carries what only the sync needs: the version's `lastUpdateTime` (verbatim, for optimistic locking),
    its state, the Elvira-owned values and the raw fields (`deleteMetadata` must echo a current value)."""
    citation = block_values(version, "citation")
    book = block_values(version, ELVIRA_BLOCK)
    pid = pid or version.get("datasetPersistentId")

    other_ids = []
    isbns: List[str] = _texts(book.get("isbn"))
    doi = issn = ccnb = None
    for item in _as_list(citation.get("otherId")):
        agency = (_sub(item, "otherIdAgency") or "").strip().upper()
        value = _sub(item, "otherIdValue")
        if not value:
            continue
        if agency == "ISBN":
            isbns.append(value)
        elif agency == "DOI" and doi is None:
            doi = normalize_doi(value) or value
        elif agency == "ISSN" and issn is None:
            issn = normalize_issn(value) or value
        elif agency in CCNB_AGENCIES and ccnb is None:
            ccnb = normalize_ccnb(value) or value
        else:
            other_ids.append({"agency": _sub(item, "otherIdAgency"), "value": value})

    normalised = []
    for value in isbns:
        isbn = normalize_isbn(value)
        if isbn and isbn not in normalised:
            normalised.append(isbn)

    descriptions = [_sub(item, "dsDescriptionValue") for item in _as_list(citation.get("dsDescription"))]
    publishers = [_sub(item, "distributorName") for item in _as_list(citation.get("distributor"))]
    producers = [_sub(item, "producerName") for item in _as_list(citation.get("producer"))]
    page_count = _text(book.get("pageCount"))

    subjects = [SourceSubject(term=term, scheme=UDC_SCHEME) for term in _texts(book.get("udc"))]
    subjects += [
        SourceSubject(term=term)
        for term in (_sub(item, "keywordValue") for item in _as_list(citation.get("keyword")))
        if term
    ]

    licence = version.get("license") if isinstance(version.get("license"), dict) else {}
    raw = {**citation, **book}
    return SourceRecord(
        record_id=pid or "",
        title=_text(citation.get("title")) or "",
        subtitle=_text(citation.get("subtitle")),
        authors=[
            SourceAuthor.parse(name)
            for name in (_sub(item, "authorName") for item in _as_list(citation.get("author")))
            if name
        ],
        identifiers={
            key: value
            for key, value in (
                ("isbn", normalised[0] if normalised else None),
                ("issn", issn),
                ("ccnb", ccnb),
                ("doi", doi),
            )
            if value
        },
        other_isbns=normalised[1:],
        # ISO 639-1 (639-2 where there is none): Dataverse's vocabulary holds English names ("Slovak").
        language=next((language_code_of(name) or name for name in _texts(citation.get("language"))), None),
        publisher=next((value for value in publishers + producers if value), None),
        published=_text(citation.get("distributionDate")) or _text(citation.get("productionDate")),
        edition=_text(book.get("edition")),
        series=_text(book.get("seriesTitle")),
        series_number=_text(book.get("seriesNumber")),
        page_count=int(page_count) if page_count and page_count.isdigit() else None,
        summary=next((value for value in descriptions if value), None),
        subjects=subjects,
        licence_url=licence.get("uri"),
        licence_name=licence.get("name"),
        links=_texts(citation.get("relatedMaterial")),
        deleted=str(version.get("versionState") or "").upper() == "DEACCESSIONED",
        datestamp=version.get("lastUpdateTime"),
        # The version the sync hands back as `expected_version` (`sourceLastUpdateTime`), verbatim.
        version=version.get("lastUpdateTime"),
        extra={
            "isbns_raw": isbns,
            "dataset_id": str(dataset_id or version.get("datasetId") or "") or None,
            "version_state": version.get("versionState"),
            "last_update_time": version.get("lastUpdateTime"),
            "elvira_entry_id": _text(book.get("elviraEntryId")),
            "elvira_url": _text(book.get("elviraUrl")),
            "lending": _text(book.get("lending")),
            "other_ids": other_ids,
            "raw_fields": raw,
        },
    )


def lending_of(entry) -> str:
    """How Elvira lends the book, for `elvira_book.lending`: `lcp` (DRM loans), `open` (public catalog) or
    `restricted` (members of the catalog only)."""
    if (entry.config or {}).get("readium_enabled"):
        return "lcp"
    return "open" if entry.catalog.is_public else "restricted"


def _published_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, PartialDate) else parse_partial_date(str(value))
    return repr(parsed) if parsed is not None else None


def entry_to_source_record(entry) -> SourceRecord:
    """An entry as a `SourceRecord` (the outbound direction). `record_id` is the dataset PID when linked;
    `extra` carries the Elvira-owned `elvira_book` values (`elvira_entry_id`, `elvira_url`, `lending`)."""
    from apps.core.models import EntryAuthor
    from apps.dataverse.services.links import portal_entry_url

    identifiers = entry.identifiers or {}
    pid = identifiers.get("dataverse_pid")
    doi = normalize_doi(identifiers.get("doi"))
    if doi and pid and doi == normalize_doi(pid):
        doi = None  # a Dataverse-imported entry's `doi` used to be the dataset's own PID: the link, not the book's
    # An invalid ISBN is kept as typed: dropping it would clear the dataset's value on the next write.
    isbn = normalize_isbn(identifiers.get("isbn")) or _text(identifiers.get("isbn"))
    links = EntryAuthor.objects.filter(entry=entry).select_related("author").order_by("position", "pk")
    return SourceRecord(
        record_id=pid or "",
        title=_text(entry.title) or "",
        authors=[SourceAuthor(surname=link.author.surname or "", name=link.author.name or "") for link in links],
        identifiers={key: value for key, value in (("isbn", isbn), ("doi", doi)) if value},
        language=entry.language.alpha2 if entry.language_id and entry.language else None,
        publisher=_text(entry.publisher),
        published=_published_text(entry.published_at),
        edition=_text(entry.edition),
        page_count=entry.page_count or None,
        summary=_text(entry.summary),
        subjects=[
            SourceSubject(term=term, scheme=scheme) for term, scheme in entry.categories.values_list("term", "scheme")
        ],
        licence_url=(entry.rights or {}).get("license_url"),
        licence_name=(entry.rights or {}).get("license_name"),
        extra={
            "elvira_entry_id": str(entry.pk),
            "elvira_url": portal_entry_url(entry.pk),
            "lending": lending_of(entry),
        },
    )


def _author_text(author: SourceAuthor) -> Optional[str]:
    surname, name = _text(author.surname) or "", _text(author.name) or ""
    if surname and name:
        return f"{surname}, {name}"
    return surname or name or None


def _terms(values) -> Optional[List[str]]:
    terms = sorted({text for text in (_text(value) for value in values) if text})
    return terms or None


def record_to_values(record: SourceRecord) -> Dict[str, Any]:
    """A `SourceRecord` (from either side) as the flat values the payload builders write:

    `title`, `authors` (["Surname, Name"], in order), `summary`, `publisher`, `published` ("YYYY[-MM[-DD]]"),
    `language` (ISO 639-1), `keywords` / `udc` (sorted terms; UDC = scheme `UDC_SCHEME`), `isbn` (ISBN-13),
    `issn`, `ccnb`, `doi` (bare), `edition`, `page_count`, and the Elvira-owned `elvira_entry_id`, `elvira_url`,
    `lending` from `record.extra`. Empty values are None. No database access."""
    isbn = record.identifiers.get("isbn")
    identifiers = record.identifiers
    return {
        "title": _text(record.full_title),
        "authors": [text for text in (_author_text(author) for author in record.authors) if text] or None,
        "summary": _text(record.summary),
        "publisher": _text(record.publisher),
        "published": _published_text(record.published),
        "language": language_code_of(record.language),
        "keywords": _terms(subject.term for subject in record.subjects if subject.scheme != UDC_SCHEME),
        "udc": _terms(subject.term for subject in record.subjects if subject.scheme == UDC_SCHEME),
        "isbn": normalize_isbn(isbn) or _text(isbn),
        "issn": normalize_issn(identifiers.get("issn")) or _text(identifiers.get("issn")),
        "ccnb": normalize_ccnb(identifiers.get("ccnb")),
        "doi": normalize_doi(record.identifiers.get("doi")),
        "edition": _text(record.edition),
        "page_count": record.page_count or None,
        "elvira_entry_id": record.extra.get("elvira_entry_id"),
        "elvira_url": record.extra.get("elvira_url"),
        "lending": record.extra.get("lending"),
    }


def _values(values) -> Dict[str, Any]:
    return record_to_values(values) if isinstance(values, SourceRecord) else dict(values)


def field_payload(type_name: str, value: Any, *, full: bool = False) -> Dict[str, Any]:
    """One field as the native API takes it. `value` is a str or a list (of str, or of subfield dicts for a
    compound field). `full` adds `multiple`/`typeClass`, which dataset creation requires."""
    _block, multiple, type_class, subfields = FIELD_SPECS[type_name]
    if type_class == "compound":
        items = []
        for item in _as_list(value):
            if not isinstance(item, dict):
                item = {subfields[0]: item}
            items.append(
                {
                    name: (
                        {"typeName": name, "multiple": False, "typeClass": "primitive", "value": sub}
                        if full
                        else {"typeName": name, "value": sub}
                    )
                    for name, sub in item.items()
                    if sub not in (None, "")
                }
            )
        value = items
    elif multiple:
        value = [str(item) for item in _as_list(value)]
    else:
        value = str(value)
    payload: Dict[str, Any] = {"typeName": type_name, "value": value}
    if full:
        payload.update({"multiple": multiple, "typeClass": type_class})
    return payload


def dataverse_values(values, remote: Optional[SourceRecord] = None) -> Dict[str, Any]:
    """A `SourceRecord` or `record_to_values()` dict -> `{typeName: value}` for every field Elvira writes. Values
    Elvira does not own are kept from `remote` (the dataset as read): further ISBNs, other `otherId` agencies,
    other related material."""
    values = _values(values)
    extra = remote.extra if remote is not None else {}
    isbns = [values["isbn"]] if values.get("isbn") else []
    isbns += [isbn for isbn in (remote.other_isbns if remote is not None else []) if isbn not in isbns]

    other_ids = []
    if values.get("isbn"):
        other_ids.append({"otherIdAgency": "ISBN", "otherIdValue": values["isbn"]})
    if values.get("issn"):
        other_ids.append({"otherIdAgency": "ISSN", "otherIdValue": values["issn"]})
    if values.get("ccnb"):
        other_ids.append({"otherIdAgency": "CCNB", "otherIdValue": values["ccnb"]})
    if values.get("doi"):
        other_ids.append({"otherIdAgency": "DOI", "otherIdValue": values["doi"]})
    other_ids += [
        {"otherIdAgency": item.get("agency"), "otherIdValue": item.get("value")}
        for item in extra.get("other_ids") or []
    ]

    related = [values["elvira_url"]] if values.get("elvira_url") else []
    related += [link for link in (remote.links if remote is not None else []) if link not in related]

    language = language_name(values.get("language"))
    return {
        "title": values.get("title"),
        "author": list(values.get("authors") or []),
        "dsDescription": [values["summary"]] if values.get("summary") else [],
        "distributor": [values["publisher"]] if values.get("publisher") else [],
        "distributionDate": values.get("published"),
        "language": [language] if language else [],
        "keyword": list(values.get("keywords") or []),
        "udc": list(values.get("udc") or []),
        "isbn": isbns,
        "otherId": other_ids,
        "edition": values.get("edition"),
        "pageCount": str(values["page_count"]) if values.get("page_count") else None,
        "elviraEntryId": values.get("elvira_entry_id"),
        "elviraUrl": values.get("elvira_url"),
        "relatedMaterial": related,
        "lending": values.get("lending"),
    }


#: Canonical sync field -> the Dataverse fields that carry it.
CANONICAL_TO_DATAVERSE: Dict[str, tuple] = {
    "title": ("title",),
    "authors": ("author",),
    "summary": ("dsDescription",),
    "publisher": ("distributor",),
    "published": ("distributionDate",),
    "language": ("language",),
    "keywords": ("keyword",),
    "udc": ("udc",),
    "isbn": ("isbn", "otherId"),
    "issn": ("otherId",),
    "ccnb": ("otherId",),
    "doi": ("otherId",),
    "edition": ("edition",),
    "page_count": ("pageCount",),
    "elvira_entry_id": ("elviraEntryId",),
    "elvira_url": ("elviraUrl", "relatedMaterial"),
    "lending": ("lending",),
}
#: Values only Elvira writes (`elvira_book.elviraEntryId`, `elviraUrl` + `relatedMaterial`, `lending`).
OWNED_VALUES = ("elvira_entry_id", "elvira_url", "lending")


def changed_values(wanted, remote: SourceRecord) -> List[str]:
    """Value names (keys of `CANONICAL_TO_DATAVERSE`) whose wanted value differs from what the dataset holds."""
    wanted, current = _values(wanted), record_to_values(remote)
    return [name for name in CANONICAL_TO_DATAVERSE if wanted.get(name) != current.get(name)]


def book_problems(record: SourceRecord) -> List[str]:
    """Q9: what stops a `book` dataset from being published: an ISBN with a bad checksum (any of `elvira_book.isbn`
    and `otherId` ISBN, as typed), or no UDC code."""
    isbns = record.extra.get("isbns_raw")
    if isbns is None:
        isbns = [value for value in [record.identifiers.get("isbn"), *record.other_isbns] if value]
    problems = []
    invalid = [isbn for isbn in isbns if not isbn_valid(isbn)]
    if invalid:
        problems.append(f"invalid ISBN {', '.join(invalid)}")
    if not any(subject.scheme == UDC_SCHEME and _text(subject.term) for subject in record.subjects):
        problems.append("no UDC code (elvira_book.udc)")
    return problems


def edit_payload(
    values, changed: List[str], remote: Optional[SourceRecord] = None
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """`(fields to set, fields to delete)` for the value names in `changed` (keys of `CANONICAL_TO_DATAVERSE`).
    `values` is a `SourceRecord` or a `record_to_values()` dict; `remote` the dataset as last read.

    `editMetadata?replace=true` cannot store an empty value, so an emptied field goes to `deleteMetadata` with
    the value Dataverse holds now (the raw field from `remote`)."""
    wanted = dataverse_values(values, remote)
    raw = (remote.extra.get("raw_fields") if remote is not None else None) or {}
    to_set, to_clear, seen = [], [], set()
    for name in changed:
        for type_name in CANONICAL_TO_DATAVERSE.get(name, ()):
            if type_name in seen:
                continue
            seen.add(type_name)
            value = wanted.get(type_name)
            if value in (None, "", []):
                if raw.get(type_name) not in (None, "", []):
                    to_clear.append({"typeName": type_name, "value": raw[type_name]})
                continue
            to_set.append(field_payload(type_name, value))
    return to_set, to_clear


def create_payload(values, *, contact_email: str, contact_name: str, licence=None) -> Dict[str, Any]:
    """The body of `POST /api/dataverses/{alias}/datasets` for a new `book` dataset (no files, Q8).

    `values`: a `SourceRecord` (its licence is used when `licence` is None) or a `record_to_values()` dict.
    `datasetContact`, `dsDescription` and `subject` are required by the citation block; `subject` is not synced
    and is sent as "Other"."""
    if licence is None and isinstance(values, SourceRecord):
        licence = {"name": values.licence_name, "uri": values.licence_url}
    values = _values(values)
    wanted = dataverse_values(values)
    wanted["datasetContact"] = [{"datasetContactName": contact_name, "datasetContactEmail": contact_email}]
    wanted["subject"] = ["Other"]
    if not wanted["dsDescription"]:
        wanted["dsDescription"] = [values.get("title") or ""]
    blocks: Dict[str, Dict[str, Any]] = {}
    for type_name, value in wanted.items():
        if value in (None, "", []):
            continue
        block = FIELD_SPECS[type_name][0]
        blocks.setdefault(block, {"fields": []})["fields"].append(field_payload(type_name, value, full=True))
    version: Dict[str, Any] = {"metadataBlocks": blocks}
    if licence and (licence.get("name") or licence.get("uri")):
        version["license"] = {key: licence[key] for key in ("name", "uri") if licence.get(key)}
    return {"datasetType": DATASET_TYPE_BOOK, "datasetVersion": version}

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

from apps.core.models import Acquisition, Language


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

    for fmt, include_month, include_day in (
        ("%Y-%m-%d", True, True),
        ("%Y-%m", True, False),
        ("%Y", False, False),
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return PartialDate(
                year=dt.year,
                month=dt.month if include_month else None,
                day=dt.day if include_day else None,
            )
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

    metadata["content"] = _build_content(metadata, files)
    metadata["citation"] = _build_citation(metadata)

    return metadata

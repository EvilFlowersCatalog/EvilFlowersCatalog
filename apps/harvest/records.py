"""IP-034: the one shape every harvest adapter produces, and identifier normalisation.

Connectors (OAI-PMH, MARC files, and those other apps register) turn whatever their source speaks into
`SourceRecord`s. Matching, applying and the two-way merge (`apps.harvest.matching`, `.apply`, `.merge`) only
ever see this shape.
Plain Python: no Django imports, so it is cheap to test and to reuse.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: Scheme of UDC (MDT) categories, as the `stu` catalog stores them.
UDC_SCHEME = "http://udcdata.info/"


@dataclass
class SourceAuthor:
    surname: str
    name: str = ""

    @classmethod
    def parse(cls, value: str) -> "SourceAuthor":
        """'Surname, Name' (catalogue order) or 'Name Surname'."""
        value = " ".join((value or "").replace(" ,", ",").split()).strip(" ,.;")
        if "," in value:
            surname, _, name = value.partition(",")
            return cls(surname=surname.strip(), name=name.strip(" ,."))
        parts = value.split(" ")
        if len(parts) == 1:
            return cls(surname=parts[0])
        return cls(surname=parts[-1], name=" ".join(parts[:-1]))


@dataclass
class SourceSubject:
    term: str
    scheme: Optional[str] = None
    label: Optional[str] = None


@dataclass
class SourceRecord:
    """A record as one source describes it. `record_id` is the source's own id (OAI identifier, MARC 001, a
    repository's persistent id); `identifiers` holds normalised bibliographic ids only (`isbn`, `issn`, `ccnb`,
    `doi`). `version` is the remote's version of the record (e.g. its last-update time), opaque to the harvester:
    the two-way sync stores it and hands it back to the connector as `expected_version`."""

    record_id: str
    title: str = ""
    subtitle: Optional[str] = None
    authors: list[SourceAuthor] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    #: Further ISBNs of the same record (other formats, MARC 020 $a repeats); never used to create.
    other_isbns: list[str] = field(default_factory=list)
    language: Optional[str] = None  # ISO 639-1 or 639-2 code, as the source has it
    publisher: Optional[str] = None
    published: Optional[str] = None  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    edition: Optional[str] = None
    series: Optional[str] = None
    series_number: Optional[str] = None
    page_count: Optional[int] = None
    summary: Optional[str] = None
    subjects: list[SourceSubject] = field(default_factory=list)
    licence_url: Optional[str] = None
    licence_name: Optional[str] = None
    links: list[str] = field(default_factory=list)
    deleted: bool = False
    datestamp: Optional[str] = None
    #: Connector-specific extras (e.g. a second internal id); not matched, not hashed into `raw_hash`.
    extra: dict[str, Any] = field(default_factory=dict)
    #: The remote's version of this record; not hashed. Connectors that can write fill it.
    version: Optional[str] = None

    @property
    def full_title(self) -> str:
        if self.subtitle and self.subtitle.lower() not in (self.title or "").lower():
            return f"{self.title}: {self.subtitle}" if self.title else self.subtitle
        return self.title

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SourceRecord":
        data = dict(data)
        data["authors"] = [SourceAuthor(**a) for a in data.get("authors") or []]
        data["subjects"] = [SourceSubject(**s) for s in data.get("subjects") or []]
        known = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in known})

    def raw_hash(self) -> str:
        """Stable hash of everything a harvest applies; an unchanged hash means nothing to do."""
        payload = self.as_dict()
        payload.pop("extra", None)
        payload.pop("datestamp", None)
        payload.pop("version", None)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# -- identifiers ---------------------------------------------------------------------------------------------

_ISBN_CHARS = re.compile(r"[^0-9Xx]")


def _isbn10_valid(digits: str) -> bool:
    if len(digits) != 10 or not digits[:9].isdigit():
        return False
    total = sum((10 - i) * int(c) for i, c in enumerate(digits[:9]))
    check = 10 if digits[9] in "Xx" else (int(digits[9]) if digits[9].isdigit() else -1)
    return (total + check) % 11 == 0


def _isbn13_valid(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == int(digits[12])


def normalize_isbn(value: Optional[str]) -> Optional[str]:
    """ISBN-13 digits (978-prefixed from a valid ISBN-10), or None when the checksum fails.

    MARC 020 $a often carries a qualifier ("80-227-1234-5 (brož.)"); only the leading ISBN is read.
    """
    if not value:
        return None
    head = re.split(r"[\s(:;]", value.strip(), maxsplit=1)[0] if value.strip() else ""
    digits = _ISBN_CHARS.sub("", head or value)
    if len(digits) == 10 and _isbn10_valid(digits):
        core = "978" + digits[:9]
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    if len(digits) == 13 and _isbn13_valid(digits):
        return digits
    return None


def isbn_valid(value: Optional[str]) -> bool:
    return normalize_isbn(value) is not None


def normalize_issn(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = re.sub(r"[^0-9Xx]", "", value)[:8].upper()
    if len(digits) != 8:
        return None
    total = sum((8 - i) * int(c) for i, c in enumerate(digits[:7]))
    check = (11 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return f"{digits[:4]}-{digits[4:]}" if digits[7] == expected else None


def normalize_doi(value: Optional[str]) -> Optional[str]:
    """Bare lower-cased DOI ("10.1234/abc"), without `doi:` or resolver prefixes."""
    if not value:
        return None
    value = value.strip()
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", value, flags=re.IGNORECASE)
    return value.lower() if value.startswith("10.") else None


def normalize_ccnb(value: Optional[str]) -> Optional[str]:
    """Czech/Slovak national bibliography number (MARC 015, e.g. 'cnb000123456'), lower-cased, no spaces."""
    if not value:
        return None
    value = re.sub(r"\s+", "", value).lower()
    return value or None


NORMALIZERS = {
    "isbn": normalize_isbn,
    "issn": normalize_issn,
    "doi": normalize_doi,
    "ccnb": normalize_ccnb,
}


def normalize_identifiers(raw: dict[str, Optional[str]]) -> dict[str, str]:
    """Keep only valid, normalised bibliographic identifiers."""
    result = {}
    for key, normalizer in NORMALIZERS.items():
        value = normalizer(raw.get(key))
        if value:
            result[key] = value
    return result


__all__ = [
    "NORMALIZERS",
    "SourceAuthor",
    "SourceRecord",
    "SourceSubject",
    "UDC_SCHEME",
    "isbn_valid",
    "normalize_ccnb",
    "normalize_doi",
    "normalize_identifiers",
    "normalize_isbn",
    "normalize_issn",
]

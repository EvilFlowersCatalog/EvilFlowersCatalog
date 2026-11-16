"""MARC 21 bibliographic records → `SourceRecord` (IP-034).

The mapping reads what Slovak and Czech library systems (ARL, Koha, Aleph) put where: `020` ISBN, `022` ISSN,
`015` ČNB, `024 7_ $2 doi`, `041`/`008` language, `100`/`700` authors, `245` title, `250` edition, `260`/`264`
imprint, `300` extent, `490`/`830` series, `520` summary, `080` UDC, `650`/`653` subjects, `540` licence,
`856` links.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

import pycountry
from pymarc import Field, Indicators, Record, Subfield

from apps.harvest.adapters.base import children, local
from apps.harvest.records import (
    UDC_SCHEME,
    SourceAuthor,
    SourceRecord,
    SourceSubject,
    normalize_ccnb,
    normalize_doi,
    normalize_isbn,
    normalize_issn,
)

#: ISBD punctuation a cataloguer leaves at the end of a subfield ("Title /", "Bratislava :", "2018.").
_TRAILING = " /:;,=+"


def clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(str(value).split()).strip(_TRAILING)
    # A full stop ends most subfields; keep it after an initial ("J.") or an abbreviation of one letter.
    if value.endswith(".") and not re.search(r"(^|\s)\w\.$", value):
        value = value[:-1].rstrip(_TRAILING)
    value = value.strip("[]") if value.startswith("[") and value.endswith("]") else value
    return value or None


def language_code(value: Optional[str]) -> Optional[str]:
    """ISO 639-1 where one exists, else ISO 639-2/T. MARC uses 639-2/B (`slo`, `cze`, `ger`)."""
    value = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", value) or value in ("und", "zxx", "mul", "   "):
        return None
    language = (
        pycountry.languages.get(alpha_2=value)
        if len(value) == 2
        else (pycountry.languages.get(bibliographic=value) or pycountry.languages.get(alpha_3=value))
    )
    if language is None:
        return None
    return getattr(language, "alpha_2", None) or language.alpha_3


def year(value: Optional[str]) -> Optional[str]:
    match = re.search(r"(1[5-9]\d\d|20\d\d)", value or "")
    return match.group(1) if match else None


# -- MARCXML -------------------------------------------------------------------------------------------------


def record_from_element(element) -> Record:
    """A MARCXML `<record>` (any namespace) as a pymarc `Record`, without pymarc's SAX reader."""
    record = Record()
    for item in element:
        name = local(item)
        if name == "leader" and item.text:
            record.leader = item.text
        elif name == "controlfield":
            record.add_field(Field(tag=item.get("tag", ""), data=item.text or ""))
        elif name == "datafield":
            subfields = [
                Subfield(code=sub.get("code", ""), value=sub.text or "") for sub in children(item, "subfield")
            ]
            record.add_field(
                Field(
                    tag=item.get("tag", ""),
                    indicators=Indicators(item.get("ind1") or " ", item.get("ind2") or " "),
                    subfields=subfields,
                )
            )
    return record


def records_in(element) -> Iterator:
    """MARCXML `<record>` elements of a `<collection>`, or the element itself."""
    if local(element) == "record":
        yield element
        return
    for item in element.iter():
        if local(item) == "record" and item is not element:
            yield item


# -- mapping -------------------------------------------------------------------------------------------------


def _first(record: Record, tag: str, code: str) -> Optional[str]:
    for field in record.get_fields(tag):
        for value in field.get_subfields(code):
            cleaned = clean(value)
            if cleaned:
                return cleaned
    return None


def _control(record: Record, tag: str) -> Optional[str]:
    fields = record.get_fields(tag)
    if not fields or fields[0].data is None:
        return None
    return fields[0].data


def to_source_record(record: Record, *, fallback_id: Optional[str], subject_scheme: str) -> SourceRecord:
    """Map one MARC 21 bibliographic record; `fallback_id` is used when it has no `001`."""
    record_id = (_control(record, "001") or "").strip() or (fallback_id or "")

    isbns: list[str] = []
    for field in record.get_fields("020"):
        for value in field.get_subfields("a"):
            isbn = normalize_isbn(value)
            if isbn and isbn not in isbns:
                isbns.append(isbn)
    identifiers = {}
    if isbns:
        identifiers["isbn"] = isbns[0]
    issn = next((normalize_issn(v) for f in record.get_fields("022") for v in f.get_subfields("a") if v), None)
    if issn:
        identifiers["issn"] = issn
    ccnb = next((normalize_ccnb(v) for f in record.get_fields("015") for v in f.get_subfields("a") if v), None)
    if ccnb:
        identifiers["ccnb"] = ccnb
    for field in record.get_fields("024"):
        if field.indicator1 == "7" and any((code or "").lower() == "doi" for code in field.get_subfields("2")):
            doi = next((normalize_doi(v) for v in field.get_subfields("a") if normalize_doi(v)), None)
            if doi:
                identifiers["doi"] = doi
                break

    language = language_code(_first(record, "041", "a"))
    if language is None:
        fixed = _control(record, "008") or ""
        language = language_code(fixed[35:38]) if len(fixed) >= 38 else None

    authors = []
    for tag in ("100", "700"):
        for field in record.get_fields(tag):
            name = clean(" ".join(field.get_subfields("a")))
            if name:
                authors.append(SourceAuthor.parse(name))

    title = _first(record, "245", "a") or ""
    subtitle = _first(record, "245", "b")

    publisher, published = None, None
    imprints = record.get_fields("264")
    imprints = [field for field in imprints if field.indicator2 == "1"] or imprints or record.get_fields("260")
    for field in imprints:
        publisher = publisher or next((clean(v) for v in field.get_subfields("b") if clean(v)), None)
        published = published or next((year(v) for v in field.get_subfields("c") if year(v)), None)
    if published is None:
        fixed = _control(record, "008") or ""
        published = year(fixed[7:11]) if len(fixed) >= 11 else None

    page_count = None
    extent = _first(record, "300", "a")
    if extent:
        match = re.search(r"(\d+)\s*(s|p|str|pages|stran)", extent, flags=re.IGNORECASE) or re.search(r"\d+", extent)
        if match:
            page_count = int(match.group(1) if match.groups() else match.group(0))

    series, series_number = _first(record, "490", "a"), _first(record, "490", "v")
    if series is None:
        series, series_number = _first(record, "830", "a"), _first(record, "830", "v")

    subjects: list[SourceSubject] = []
    seen = set()
    for tag, scheme in (("080", UDC_SCHEME), ("650", subject_scheme), ("653", subject_scheme)):
        for field in record.get_fields(tag):
            for value in field.get_subfields("a"):
                term = clean(value)
                if term and (scheme, term) not in seen:
                    seen.add((scheme, term))
                    subjects.append(SourceSubject(term=term[:255], scheme=scheme))

    links = []
    for field in record.get_fields("856"):
        for value in field.get_subfields("u"):
            value = (value or "").strip()
            if value and value not in links:
                links.append(value)

    summaries = [clean(v) for f in record.get_fields("520") for v in f.get_subfields("a")]
    summary = "\n\n".join(value for value in summaries if value) or None

    return SourceRecord(
        record_id=record_id,
        title=title,
        subtitle=subtitle,
        authors=authors,
        identifiers=identifiers,
        other_isbns=isbns[1:],
        language=language,
        publisher=publisher,
        published=published,
        edition=_first(record, "250", "a"),
        series=series,
        series_number=series_number,
        page_count=page_count,
        summary=summary,
        subjects=subjects,
        licence_url=next((v.strip() for f in record.get_fields("540") for v in f.get_subfields("u") if v), None),
        licence_name=_first(record, "540", "a"),
        links=links,
    )


__all__ = ["clean", "language_code", "record_from_element", "records_in", "to_source_record", "year"]

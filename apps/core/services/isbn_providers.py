"""Bibliographic metadata providers by ISBN (#202), the drivers of `apps.core.services.isbn_metadata`'s queue.

Each provider answers one ISBN with a `Record` that may be partial (no year, no publisher, editors but no
authors); the queue merges records field by field. All are free and need no subscription:

| Name | Source | Notes |
|---|---|---|
| `crossref` | api.crossref.org `works?filter=isbn:` | DOIs of academic books; keeps editors apart from authors. `EVILFLOWERS_CROSSREF_MAILTO` joins the polite pool. |
| `openlibrary` | openlibrary.org `api/books?jscmd=data` | Wide, mostly English coverage. |
| `snk` | Slovak National Library union catalogue, Z39.50 (`EVILFLOWERS_SNK_Z3950`, `z3950.snk.sk:1111/clas01`), MARC21 | Slovak publications; relator codes (`$4 aut`, `edt`) tell authors from editors. |
| `googlebooks` | googleapis.com Books v1 | Needs `EVILFLOWERS_GOOGLE_BOOKS_API_KEY`: keyless requests share a daily quota of 0. Skipped without a key. |
| `openl`, `goob`, `wiki` | isbnlib's services | Kept for configurations written before the drivers. |

A provider that fails (network, quota, malformed answer) raises `ProviderError`; one that does not know the ISBN
returns None.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import requests
from django.conf import settings

from apps.harvest.records import SourceAuthor

logger = logging.getLogger(__name__)

_YEAR = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")
#: Crossref types of a whole book; a chapter shares the book's ISBN.
CROSSREF_BOOKS = ("book", "edited-book", "monograph", "reference-book", "book-set", "book-series")
#: MARC relator codes (`$4`) and terms (`$e`) of editors, and of authors.
MARC_EDITORS = ("edt", "ed.", "ed", "editor", "zostavovateľ", "zost.", "red.", "redaktor", "pp")
MARC_AUTHORS = ("aut", "author", "autor", "cre")


class ProviderError(Exception):
    """The provider could not be asked or answered something unusable."""


@dataclass
class Record:
    provider: str
    authors: List[SourceAuthor] = field(default_factory=list)
    editors: List[SourceAuthor] = field(default_factory=list)
    publisher: Optional[str] = None
    year: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    doi: Optional[str] = None


def _year(value) -> Optional[str]:
    found = _YEAR.search(str(value or ""))
    return found.group(1) if found else None


def _clean(value: Optional[str], limit: int = 255) -> Optional[str]:
    text = " ".join(str(value or "").split()).strip(" ,;:/.")
    return text[:limit] or None


def _people(names) -> List[SourceAuthor]:
    people = [SourceAuthor.parse(_clean(name) or "") for name in names or []]
    return [person for person in people if person.surname]


def _digits(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def _timeout() -> float:
    return settings.EVILFLOWERS_ISBN_METADATA_TIMEOUT


def _user_agent() -> str:
    mailto = settings.EVILFLOWERS_CROSSREF_MAILTO
    return f"EvilFlowersCatalog/1 (+mailto:{mailto})" if mailto else "EvilFlowersCatalog/1"


def _get(url: str, params: dict) -> dict:
    try:
        response = requests.get(url, params=params, timeout=_timeout(), headers={"User-Agent": _user_agent()})
    except requests.RequestException as error:
        raise ProviderError(str(error)) from error
    if response.status_code == 404:
        return {}
    if response.status_code != 200:
        raise ProviderError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as error:
        raise ProviderError("not JSON") from error


# -- Crossref -----------------------------------------------------------------------------------------------


def _crossref_names(people) -> List[SourceAuthor]:
    found = []
    for person in people or []:
        surname = _clean(person.get("family"))
        if surname:
            found.append(SourceAuthor(name=_clean(person.get("given")) or "", surname=surname))
        elif person.get("name"):
            found.extend(_people([person["name"]]))
    return found


def crossref(isbn: str) -> Optional[Record]:
    params = {
        "filter": f"isbn:{isbn}",
        "rows": 10,
        "select": "DOI,type,title,author,editor,publisher,issued,ISBN",
    }
    if settings.EVILFLOWERS_CROSSREF_MAILTO:
        params["mailto"] = settings.EVILFLOWERS_CROSSREF_MAILTO
    items = ((_get("https://api.crossref.org/works", params).get("message") or {}).get("items")) or []
    if not items:
        return None
    books = [item for item in items if item.get("type") in CROSSREF_BOOKS]
    # The book record itself, best with this exact ISBN; else a chapter, which still carries the book's publisher,
    # year and editors (never its own authors and title).
    exact = [item for item in books if isbn in {_digits(value) for value in item.get("ISBN") or []}]
    item = (exact or books or [None])[0]
    if item is not None:
        return Record(
            provider="crossref",
            authors=_crossref_names(item.get("author")),
            editors=_crossref_names(item.get("editor")),
            publisher=_clean(item.get("publisher")),
            year=_year(((item.get("issued") or {}).get("date-parts") or [[None]])[0][0]),
            title=_clean(" ".join(item.get("title") or []), 500),
            doi=item.get("DOI"),
        )
    chapter = items[0]
    return Record(
        provider="crossref",
        editors=_crossref_names(chapter.get("editor")),
        publisher=_clean(chapter.get("publisher")),
        year=_year(((chapter.get("issued") or {}).get("date-parts") or [[None]])[0][0]),
    )


# -- Open Library -------------------------------------------------------------------------------------------


def openlibrary(isbn: str) -> Optional[Record]:
    payload = _get("https://openlibrary.org/api/books", {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
    book = payload.get(f"ISBN:{isbn}")
    if not book:
        return None
    title = " : ".join(part for part in (book.get("title"), book.get("subtitle")) if part)
    return Record(
        provider="openlibrary",
        authors=_people(author.get("name") for author in book.get("authors") or []),
        publisher=_clean(((book.get("publishers") or [{}])[0]).get("name")),
        year=_year(book.get("publish_date")),
        title=_clean(title, 500),
    )


# -- Google Books -------------------------------------------------------------------------------------------


def googlebooks(isbn: str) -> Optional[Record]:
    key = settings.EVILFLOWERS_GOOGLE_BOOKS_API_KEY
    if not key:
        # Keyless requests share a daily quota of 0 (HTTP 429): asking without a key only costs time.
        return None
    payload = _get("https://www.googleapis.com/books/v1/volumes", {"q": f"isbn:{isbn}", "key": key})
    items = payload.get("items") or []
    if not items:
        return None
    info = items[0].get("volumeInfo") or {}
    title = " : ".join(part for part in (info.get("title"), info.get("subtitle")) if part)
    return Record(
        provider="googlebooks",
        authors=_people(info.get("authors")),
        publisher=_clean(info.get("publisher")),
        year=_year(info.get("publishedDate")),
        title=_clean(title, 500),
        language=_clean(info.get("language"), 10),
    )


# -- Slovak National Library (Z39.50) -----------------------------------------------------------------------


def _snk_endpoint() -> tuple:
    """`(host, port, database)` from `host:port/database`."""
    value = settings.EVILFLOWERS_SNK_Z3950
    address, _, database = value.partition("/")
    host, _, port = address.partition(":")
    return host, int(port or 210), database or "Default"


def _marc_role(field) -> Optional[str]:
    """`author`, `editor` or None (reviewer, illustrator, ...) of a MARC 100/700 field."""
    codes = [code.strip().lower() for code in field.get_subfields("4")]
    terms = [_clean(term, 50).lower() for term in field.get_subfields("e") if _clean(term, 50)]
    relators = codes + terms
    if not relators:
        return "author" if field.tag == "100" else None
    if any(relator in MARC_EDITORS for relator in relators):
        return "editor"
    if any(relator in MARC_AUTHORS for relator in relators):
        return "author"
    return None


def parse_marc(raw: bytes, provider: str = "snk") -> Optional[Record]:
    import pymarc

    try:
        record = pymarc.Record(data=raw, to_unicode=True, force_utf8=raw[9:10] == b"a")
    except Exception as error:  # noqa: BLE001 - pymarc raises a family of its own
        raise ProviderError(f"unreadable MARC record: {error}") from error
    authors, editors = [], []
    for field in record.get_fields("100", "700"):
        role = _marc_role(field)
        names = _people(field.get_subfields("a"))
        if role == "author":
            authors.extend(names)
        elif role == "editor":
            editors.extend(names)
    publisher, year = None, None
    for field in record.get_fields("264", "260"):
        if field.tag == "264" and field.indicator2 not in ("1", " "):
            continue  # 264 _0 production, _2 distribution, _4 copyright date
        publisher = publisher or _clean(" ".join(field.get_subfields("b")))
        year = year or _year(" ".join(field.get_subfields("c")))
    fixed = record.get_fields("008")
    data = fixed[0].data if fixed else ""
    year = year or _year(data[7:11])
    title = None
    for field in record.get_fields("245"):
        title = _clean(" ".join(field.get_subfields("a", "b")), 500)
    language = _clean(data[35:38], 3) if len(data) >= 38 else None
    return Record(
        provider=provider,
        authors=authors,
        editors=editors,
        publisher=publisher,
        year=year,
        title=title,
        language=language,
    )


def snk(isbn: str) -> Optional[Record]:
    from apps.core.services import z3950

    host, port, database = _snk_endpoint()
    try:
        records = z3950.search(host, port, database, isbn, count=1, timeout=_timeout())
    except z3950.Z3950Error as error:
        raise ProviderError(str(error)) from error
    return parse_marc(records[0]) if records else None


# -- isbnlib (before the drivers) ---------------------------------------------------------------------------


def _isbnlib(service: str) -> Callable[[str], Optional[Record]]:
    def provider(isbn: str) -> Optional[Record]:
        import isbnlib

        try:
            raw = isbnlib.meta(isbn, service=service)
        except Exception as error:  # noqa: BLE001 - isbnlib raises its own and requests' errors alike
            raise ProviderError(str(error)) from error
        if not raw:
            return None
        return Record(
            provider=service,
            authors=_people(raw.get("Authors")),
            publisher=_clean(raw.get("Publisher")),
            year=_year(raw.get("Year")),
            title=_clean(raw.get("Title"), 500),
            language=_clean(raw.get("Language"), 10),
        )

    return provider


PROVIDERS: Dict[str, Callable[[str], Optional[Record]]] = {
    "crossref": crossref,
    "openlibrary": openlibrary,
    "snk": snk,
    "googlebooks": googlebooks,
    "openl": _isbnlib("openl"),
    "goob": _isbnlib("goob"),
    "wiki": _isbnlib("wiki"),
}

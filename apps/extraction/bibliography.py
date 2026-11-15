"""Reference lists (#178): the works a publication cites, parsed from its extracted text.

The extraction keeps a book's "References" section as paragraphs, but its paragraph breaks do not follow the
items: one paragraph holds several references and one reference runs over two paragraphs. So the section's text
is joined and split again, by the list's own structure:

- **numbered** (`1. Gurdon JB. …`, `[12] …`): the markers that count up 1, 2, 3 … are the item starts;
- **author-year** (APA `Kates, R. W. (2011). …`, Springer `Kates RW (2011) …`): every year in parentheses
  belongs to one item, and the item starts where the author list before it starts.

From each item: authors, year, title (the sentence after the year, or after the authors in Vancouver style),
DOI and ISBN. A book has a reference list per chapter as often as one at the end; each block is parsed alone,
and an in-text marker (`[12]`, `(Smith 2019)`, `Smith et al. (2019)`) is linked to an item of the next block
after its paragraph: chapter references follow the chapter.

`build(run)` stores the items as `Bibitem` rows and the markers as `Reference` rows of kind `bibitem`, matches
items to catalog entries by DOI or ISBN (`CITES` edges in the graph) and is idempotent. Crossref metadata for
items with a DOI is fetched by `enrich_run` (Celery).
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Headings of a reference list (accents folded, lower case), and headings that only look like one.
SECTION = re.compile(
    r"^\s*(?:[0-9.]+\s*)?(?:references?|bibliography|bibliografia|bibliografie|literature cited|works cited|"
    r"literatur(?:verzeichnis)?|literatura|pouzita literatura|zoznam (?:pouzitej )?literatury|zdroje|"
    r"cited literature|sources)\s*:?\s*$",
    re.IGNORECASE,
)
#: A paragraph in a reference section that is not a reference (Springer's licence boilerplate, running heads).
NOT_A_REFERENCE = re.compile(r"creative commons|open access this (chapter|book|article)", re.IGNORECASE)

#: Characters of an item kept on a row.
RAW_CHARS = 2000
#: Items per block; anything longer is not a reference list.
MAX_ITEMS = 2000

#: A year after the authors: `(2011)`, `(2011a)`, `(2011, March 3)`, or Chicago's bare `2011.`.
YEAR = re.compile(r"\((?:19|20)\d{2}[a-z]?(?:[,;][^)]{0,20})?\)|(?<=[.,]\s)(?:19|20)\d{2}[a-z]?\.(?=\s)")
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
ISBN = re.compile(r"ISBN(?:-1[03])?:?\s*((?:97[89][\s-]?)?(?:\d[\s-]?){9}[\dX])", re.IGNORECASE)
NUMBER_MARKER = re.compile(r"(?:(?<=\s)|^)(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+(?=[^\W\d_]|\[)", re.UNICODE)

_NAME = r"[^\W\d_][\w'’\-]*(?:\s(?:van|von|de|der|da|di|du|la|le|del|dos|ten|ter)\s?[\w'’\-]+)*"
_INITIALS = r"(?:[^\W\d_]\.\s?-?){1,4}"
#: One author of an author-year list: `Kates, R. W.`, `Kates RW`, `Kates, R.`, `van Dijk, T. A.`, and Chicago's
#: later authors `S. Collins`.
_AUTHOR = rf"(?:{_NAME},\s?{_INITIALS}|{_NAME}\s[^\W\d_a-z]{{1,4}}(?![\w'])|{_INITIALS}\s?{_NAME})"
AUTHOR_LIST = re.compile(
    rf"^(?:{_AUTHOR}(?:,?\s(?:&|and|und|a)\s|,\s|;\s)?)+(?:,?\s?et al\.?)?,?\s*"
    rf"(?:\((?:Eds?|Hrsg|eds?)\.?\)\.?\s*|(?:eds?|Hrsg)\.\s*)?\.?\s*$",
    re.UNICODE,
)
#: Vancouver's author list: up to the first `. ` after initials (`Gurdon JB.`) or `et al.`.
VANCOUVER_AUTHORS = re.compile(r"^(.{2,600}?(?:\s[A-Z]{1,3}|et al))\.\s+(?=\S)")
#: LNCS's author list: `Adee, S.:` `Lysne, O., Hole, K.J., et al.:`
COLON_AUTHORS = re.compile(r"^(.{2,400}?):\s+(?=\S)")
#: An institutional author: `FAO.`, `World Bank.`, `Department of Water Affairs (DWAF).`
INSTITUTION = re.compile(r"^[^\W\d_][\w&.,'’()\-– ]{1,100}?\.?\s*$", re.UNICODE)
#: Where the previous item may end: after a sentence, a page range, a DOI or URL, or a closing bracket.
ITEM_END = re.compile(r"(?:[.?!)\]]|\d|/[\w.\-]+)\s+(?=[^\W\d_])", re.UNICODE)

#: In-text markers.
NUMBERED_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]")
AUTHOR_YEAR_CITATION = re.compile(
    r"([^\W\d_][\w'’\-]+)(?:\s+(?:et al\.?|and|&)\s*(?:[^\W\d_][\w'’\-]+)?)?,?\s*\(?((?:19|20)\d{2})[a-z]?\)?",
    re.UNICODE,
)


@dataclass
class Item:
    raw: str
    #: Offset of the item in the block's text (for its page).
    start: int
    number: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    title: Optional[str] = None
    container: Optional[str] = None
    doi: Optional[str] = None
    isbn: Optional[str] = None

    @property
    def first_author(self) -> Optional[str]:
        return self.authors[0] if self.authors else None

    def csl(self) -> dict:
        csl = {
            "type": (
                "book" if self.isbn and not self.container else "article-journal" if self.container else "document"
            ),
            "title": self.title,
            "author": [_csl_name(name) for name in self.authors],
            "issued": {"date-parts": [[self.year]]} if self.year else None,
            "container-title": self.container,
            "DOI": self.doi,
            "ISBN": self.isbn,
            "citation-number": self.number,
        }
        return {key: value for key, value in csl.items() if value not in (None, [], "")}


def _csl_name(name: str) -> dict:
    name = name.strip(" ,;")
    if "," in name:
        family, given = name.split(",", 1)
        return {"family": family.strip(), "given": given.strip()}
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].replace(".", "").isupper() and len(parts[1]) <= 4:
        return {"family": parts[0], "given": parts[1]}
    return {"literal": name}


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def is_reference_section(heading: Optional[str]) -> bool:
    return bool(heading) and bool(SECTION.match(fold(heading)))


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    # Hyphenation at a line break ("stu- dents") as the extraction leaves it.
    text = re.sub(r"(\w)- (?=[a-zà-ž])", r"\1", text)
    return " ".join(text.split())


# -- splitting -------------------------------------------------------------------------------------------------


Marker = Tuple[int, int, int]


def _chain(markers: List[Marker], first: int) -> List[Marker]:
    """The markers that count up from `markers[first]`, in order."""
    chain = [markers[first]]
    expected = markers[first][1] + 1
    for marker in markers[first + 1 :]:
        if marker[1] == expected:
            chain.append(marker)
            expected += 1
    return chain


def _numbered_chains(markers: List[Marker]) -> List[List[Marker]]:
    """The numbered lists among the markers. A block may hold several (chapter endnotes, then another chapter's
    references): each marker `1` that starts a run of three begins a list of its own, which ends where the next one
    begins. A block that starts mid-list (continued from the page before) has one list, of at least five."""
    starts = [index for index, marker in enumerate(markers) if marker[1] <= 1 and len(_chain(markers, index)) >= 3]
    if starts:
        chains = []
        for position, first in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(markers)
            chains.append(_chain(markers[:end], first))
        return chains
    best: List[Marker] = []
    for first in range(len(markers)):
        chain = _chain(markers, first)
        if len(chain) > len(best):
            best = chain
        if len(best) * 2 > len(markers) - first:
            break
    # "see 2. and 3." in a sentence is not a reference list.
    return [best] if len(best) >= 5 else []


def _numbered(text: str) -> Optional[List[Item]]:
    """Items of the numbered lists of a block, or None when the block is not (mostly) numbered."""
    markers = [
        (match.start(), int(match.group(1) or match.group(2)), match.end()) for match in NUMBER_MARKER.finditer(text)
    ]
    chains = _numbered_chains(markers)
    numbered = sum(len(chain) for chain in chains)
    # Three endnotes before an author-year list (`1. For the sake of simplicity …` then `Albæk, E. (2007) …`): the
    # years outnumber the markers, so the block is parsed as author-year.
    if not numbered or numbered * 2 < len(YEAR.findall(text)):
        return None
    items = []
    for chain in chains:
        for index, (position, number, body) in enumerate(chain):
            end = chain[index + 1][0] if index + 1 < len(chain) else len(text)
            following = [other[0] for other in chains if other[0][0] > position]
            if index + 1 == len(chain) and following:
                end = min(end, following[0][0])
            raw = text[body:end].strip()
            if raw:
                items.append(Item(raw=raw, start=position, number=str(number)))
    return items


def _author_start(window: str) -> int:
    """Where the author list that ends `window` (right before a year) starts; -1 if it does not look like one."""
    candidates = [0] + [match.end() for match in ITEM_END.finditer(window)]
    for start in candidates:
        tail = window[start:].strip()
        if tail and len(tail) <= 400 and AUTHOR_LIST.match(tail):
            return start
    # Springer ends an item without a full stop ("… Press, Oxford Barro RJ (2000)"): any word start near the year.
    floor = max(0, len(window) - 400)
    for match in re.finditer(r"(?<=\s)(?=[^\W\d_])", window[floor:]):
        tail = window[floor + match.start() :].strip()
        if AUTHOR_LIST.match(tail):
            return floor + match.start()
    # An institution as author ("FAO. (2016)"): the last sentence before the year.
    for start in reversed(candidates):
        if _institution(window[start:].strip()):
            return start
    return -1


#: Lower-case words an institution's name may contain ("Ministry of Education of the People's Republic").
_NAME_WORDS = {"of", "and", "for", "the", "on", "in", "de", "des", "du", "la", "le", "für", "und", "der", "a", "&"}


def _institution(tail: str) -> bool:
    """A name, not prose: short, every word capitalised but the joining ones ("of", "and", "für")."""
    if not tail or len(tail) > 100 or not INSTITUTION.match(tail):
        return False
    words = re.findall(r"[^\s,.;()]+", tail)
    if not words or len(words) > 12:
        return False
    return all(word[0].isupper() or word[0].isdigit() or word.lower() in _NAME_WORDS for word in words)


def _author_year(text: str) -> List[Item]:
    years = list(YEAR.finditer(text))
    starts = []
    previous_end = 0
    for match in years:
        window = text[previous_end : match.start()]
        offset = _author_start(window)
        if offset < 0:
            continue
        starts.append(previous_end + offset)
        previous_end = match.end()
    items = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        raw = text[start:end].strip()
        if raw:
            items.append(Item(raw=raw, start=start))
    return items


def split(text: str) -> List[Item]:
    text = _clean(text)
    items = _numbered(text)
    if items is None:
        items = _author_year(text)
    elif items:
        # Endnotes, then an author-year list: the last numbered item swallowed the list. Split it out.
        last = items[-1]
        body = text.find(last.raw, last.start)
        tail = _author_year(last.raw) if len(YEAR.findall(last.raw)) >= 2 else []
        if len(tail) >= 2 and body >= 0:
            last.raw = last.raw[: tail[0].start].strip()
            for item in tail:
                item.start += body
            items = [item for item in items if item.raw] + tail
    for item in items[:MAX_ITEMS]:
        _fields(item)
    return items[:MAX_ITEMS]


# -- fields ----------------------------------------------------------------------------------------------------


def _sentence(text: str) -> Tuple[Optional[str], str]:
    """The first sentence of `text` and the rest. A sentence ends at `. ` `? ` `! ` not inside initials."""
    text = text.strip().lstrip(".:,; ")
    for match in re.finditer(r"[.?!](?:[”\"’'])?\s+(?=\S)", text):
        before = text[: match.start()]
        # "In J. Smith", "U.S. phones": an initial or an abbreviation, not the end of a title.
        if re.search(r"(?:^|[\s.])[^\W\d_]$", before) and len(before) > 3:
            continue
        return before.strip(" “”\"'’") or None, text[match.end() :]
    return (text.strip(" .“”\"'’") or None), ""


def _authors(segment: str) -> List[str]:
    segment = re.sub(r"\s*\((?:Eds?|Hrsg|eds?)\.?\)\.?", "", segment).strip(" .,")
    segment = re.sub(r",?\s*et al\.?$", "", segment)
    if not segment:
        return []
    parts = re.split(r",\s(?=[^\W\d_][\w'’\-]+(?:,|\s[^\W\d_a-z]{1,4}(?!\w)))|;\s|,?\s(?:&|and|und)\s", segment)
    names = []
    for part in parts:
        part = part.strip(" ,")
        if part:
            names.append(part)
    return names[:50]


def _fields(item: Item) -> None:
    raw = item.raw
    # A DOI broken at a line end: "doi:10. 1016/j.marpolbul…", "10.1016/j.wasman. 2005.03.007".
    joined = re.sub(r"\b10\.\s(\d{4,9}/)", r"10.\1", raw)
    joined = re.sub(r"(10\.\d{4,9}/\S*[./])\s+(?=[\w(])", r"\1", joined)
    doi = DOI.search(joined)
    if doi:
        item.doi = doi.group(1).rstrip(".,;)]").lower()[:255]
    isbn = ISBN.search(raw)
    if isbn:
        item.isbn = re.sub(r"[\s-]", "", isbn.group(1)).upper()
    year = YEAR.search(raw)
    colon = COLON_AUTHORS.match(raw)
    vancouver = VANCOUVER_AUTHORS.match(raw)
    if year and year.start() <= 400 and not (colon and colon.end() < year.start()):
        item.year = int(re.search(r"\d{4}", year.group(0)).group(0))
        item.authors = _authors(raw[: year.start()])
        title, rest = _sentence(raw[year.end() :])
    else:
        if colon and (not vancouver or colon.end() <= vancouver.end()):
            # LNCS: "Adee, S.: The hunt for the kill switch. IEEE Spectr. 45(5), 34–39 (2008)"
            authors, rest = colon.group(1), raw[colon.end() :]
        elif vancouver:
            # Vancouver: "Gurdon JB. The developmental capacity …. J Embryol Exp Morphol. 1962;10:622–40."
            authors, rest = vancouver.group(1), raw[vancouver.end() :]
        else:
            authors, rest = _sentence(raw)
        item.authors = _authors(authors or "")
        title, rest = _sentence(rest)
        found = re.findall(r"\b((?:19|20)\d{2})\b", rest)
        if found:
            item.year = int(found[-1] if "(" in rest else found[0])
    if title and len(title) >= 3 and not DOI.match(title) and not title.lower().startswith(("http", "retrieved")):
        item.title = title[:500]
    container, _rest = _sentence(rest)
    if container and not re.match(r"^(?:https?:|doi|retrieved|pp?\.)", container, re.IGNORECASE):
        item.container = re.sub(r"\s*\d+.*$", "", container).strip(" ,") or None


# -- runs ------------------------------------------------------------------------------------------------------


@dataclass
class Block:
    """One reference list: consecutive paragraphs under a reference heading."""

    paragraphs: list
    text: str = ""
    #: `(offset, page)` where each paragraph starts in `text`.
    offsets: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def first_seq(self) -> int:
        return self.paragraphs[0].seq

    def page_at(self, offset: int) -> int:
        page = self.offsets[0][1]
        for start, paragraph_page in self.offsets:
            if start > offset:
                break
            page = paragraph_page
        return page


def blocks(paragraphs: Iterable) -> List[Block]:
    """The reference lists of a run's paragraphs (in `seq` order)."""
    result: List[Block] = []
    current: Optional[Block] = None
    for paragraph in paragraphs:
        if not is_reference_section(paragraph.section):
            current = None
            continue
        if NOT_A_REFERENCE.search(paragraph.text or ""):
            continue
        if current is None:
            current = Block(paragraphs=[])
            result.append(current)
        current.paragraphs.append(paragraph)
    for block in result:
        parts, offset = [], 0
        for paragraph in block.paragraphs:
            text = _clean(paragraph.text)
            block.offsets.append((offset, paragraph.page))
            parts.append(text)
            offset += len(text) + 1
        block.text = " ".join(parts)
    return result


def _key(entry_id, raw: str, ordinal: int) -> str:
    from apps.extraction.locators import _digest, normalise

    parts = [entry_id, "bibitem", normalise(raw)[:300]]
    return _digest(*(parts + [ordinal] if ordinal else parts))


def _normal_isbn(value: Optional[str]) -> Optional[str]:
    digits = re.sub(r"[^0-9X]", "", (value or "").upper())
    return digits if len(digits) in (10, 13) else None


def _catalog_index() -> Tuple[Dict[str, str], Dict[str, str]]:
    """`({doi: entry_id}, {isbn: entry_id})` of the whole catalog (about a thousand entries)."""
    from apps.core.models import Entry

    dois, isbns = {}, {}
    for pk, identifiers in Entry.objects.exclude(identifiers__isnull=True).values_list("pk", "identifiers"):
        identifiers = identifiers or {}
        doi = (identifiers.get("doi") or "").lower().strip()
        for prefix in ("doi:", "https://doi.org/", "http://doi.org/"):
            if doi.startswith(prefix):
                doi = doi[len(prefix) :]
        if doi:
            dois[doi] = str(pk)
        isbn = _normal_isbn(identifiers.get("isbn"))
        if isbn:
            isbns[isbn] = str(pk)
    return dois, isbns


def build(run) -> Dict[str, int]:
    """Parse the run's reference lists into `Bibitem` rows and in-text markers into `Reference` rows (idempotent)."""
    from collections import Counter

    from django.db import transaction

    from apps.extraction.models import Bibitem, ExtractionRun, Reference

    entry_id = run.acquisition.entry_id
    paragraphs = list(run.paragraphs.order_by("seq").only("id", "seq", "page", "section", "text"))
    found = blocks(paragraphs)
    dois, isbns = _catalog_index()

    rows: List[Bibitem] = []
    per_block: List[List[Bibitem]] = []
    seen: Counter = Counter()
    for block_index, block in enumerate(found):
        current = []
        for seq, item in enumerate(split(block.text)):
            identity = item.raw[:300]
            ordinal = seen[identity]
            seen[identity] += 1
            match = dois.get(item.doi or "") or isbns.get(_normal_isbn(item.isbn) or "")
            row = Bibitem(
                run=run,
                block=block_index,
                seq=len(rows),
                number=item.number,
                page=block.page_at(item.start),
                raw=item.raw[:RAW_CHARS],
                csl=item.csl(),
                doi=item.doi,
                isbn=_normal_isbn(item.isbn),
                entry_id=match if match and match != str(entry_id) else None,
                locator_key=_key(entry_id, item.raw, ordinal),
            )
            row._first_author = fold(_csl_name(item.first_author).get("family", "")) if item.first_author else ""
            row._year = item.year
            rows.append(row)
            current.append(row)
        per_block.append(current)

    markers = _markers(paragraphs, found, per_block)
    with transaction.atomic():
        Reference.objects.filter(run=run, kind=Reference.Kind.BIBITEM).delete()
        Bibitem.objects.filter(run=run).delete()
        Bibitem.objects.bulk_create(rows, batch_size=500)
        Reference.objects.bulk_create(
            [
                Reference(
                    run=run,
                    paragraph_id=paragraph_id,
                    bibitem=row,
                    kind=Reference.Kind.BIBITEM,
                    anchor=anchor,
                    number=row.number or "",
                )
                for paragraph_id, row, anchor in markers
            ],
            batch_size=500,
        )
        counts = {
            "blocks": len(found),
            "items": len(rows),
            "titled": sum(1 for row in rows if row.csl.get("title")),
            "matched": sum(1 for row in rows if row.entry_id),
            "markers": len(markers),
        }
        # As `references.build` does: the dev measurement of #178 is one query over `provenance`.
        provenance = dict(ExtractionRun.objects.filter(pk=run.pk).values_list("provenance", flat=True).first() or {})
        provenance["bibliography"] = counts
        ExtractionRun.objects.filter(pk=run.pk).update(provenance=provenance)
    return {
        "blocks": len(found),
        "items": len(rows),
        "titled": sum(1 for row in rows if row.csl.get("title")),
        "matched": sum(1 for row in rows if row.entry_id),
        "markers": len(markers),
    }


#: In-text markers stored per run at most.
MAX_MARKERS = 20000


def _markers(paragraphs, found: List[Block], per_block: List[list]) -> List[tuple]:
    """`(paragraph_id, bibitem, anchor)` for the in-text markers, each linked to the next reference list."""
    if not found:
        return []
    block_starts = [block.first_seq for block in found]
    in_blocks = {paragraph.pk for block in found for paragraph in block.paragraphs}
    numbered_maps = [{row.number: row for row in rows if row.number} for rows in per_block]
    author_maps = []
    for rows in per_block:
        index: Dict[Tuple[str, int], object] = {}
        for row in rows:
            if row._first_author and row._year:
                index.setdefault((row._first_author, row._year), row)
        author_maps.append(index)

    result = []
    for paragraph in paragraphs:
        if paragraph.pk in in_blocks or not paragraph.text:
            continue
        target = next((index for index, start in enumerate(block_starts) if start > paragraph.seq), len(found) - 1)
        numbers, authors = numbered_maps[target], author_maps[target]
        linked = set()
        if numbers:
            for match in NUMBERED_CITATION.finditer(paragraph.text):
                for number in _expand(match.group(1)):
                    row = numbers.get(number)
                    if row is not None and row.pk not in linked:
                        linked.add(row.pk)
                        result.append((paragraph.pk, row, match.group(0)[:100]))
        if authors:
            for match in AUTHOR_YEAR_CITATION.finditer(paragraph.text):
                row = authors.get((fold(match.group(1)), int(match.group(2))))
                if row is not None and row.pk not in linked:
                    linked.add(row.pk)
                    result.append((paragraph.pk, row, match.group(0)[:100]))
        if len(result) >= MAX_MARKERS:
            break
    return result[:MAX_MARKERS]


def _expand(group: str) -> List[str]:
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        bounds = re.split(r"\s*[–\-]\s*", part)
        if (
            len(bounds) == 2
            and bounds[0].isdigit()
            and bounds[1].isdigit()
            and 0 < int(bounds[1]) - int(bounds[0]) < 50
        ):
            numbers.extend(str(number) for number in range(int(bounds[0]), int(bounds[1]) + 1))
        elif part.isdigit():
            numbers.append(part)
    return numbers


# -- Crossref --------------------------------------------------------------------------------------------------


def _from_crossref(message: dict) -> dict:
    csl = {
        "title": (message.get("title") or [None])[0],
        "container-title": (message.get("container-title") or [None])[0],
        "publisher": message.get("publisher"),
        "type": message.get("type"),
        "author": [
            {key: value for key, value in (("family", person.get("family")), ("given", person.get("given"))) if value}
            for person in message.get("author") or []
            if person.get("family")
        ],
        "issued": message.get("issued"),
        "ISBN": (message.get("ISBN") or [None])[0],
    }
    return {key: value for key, value in csl.items() if value not in (None, [], "")}


def enrich_run(run_id, limit: int = 200) -> Dict[str, int]:
    """Crossref metadata for the run's items with a DOI not enriched yet; an in-library match's licence too."""
    from apps.content import rights
    from apps.core import settings_store
    from apps.core.models import Entry
    from apps.extraction.models import Bibitem

    from django.utils import timezone

    if not settings_store.get("content.crossref_enabled"):
        return {"enriched": 0}
    counts = {"enriched": 0, "rights": 0}
    for row in Bibitem.objects.filter(run_id=run_id, doi__isnull=False, crossref_at__isnull=True)[:limit]:
        try:
            message = rights.crossref_work(row.doi, strict=True)
        except rights.CrossrefUnavailable:
            # Not stamped: the next post-processing of the run asks again.
            counts["unavailable"] = True
            break
        row.crossref_at = timezone.now()
        if message:
            row.csl = {**row.csl, **_from_crossref(message), "source": "crossref"}
            counts["enriched"] += 1
            if row.entry_id:
                entry = Entry.objects.filter(pk=row.entry_id).only("id", "rights").first()
                if entry is not None and rights.apply(entry, rights.from_crossref_message(message)):
                    counts["rights"] += 1
        row.save(update_fields=["csl", "crossref_at", "updated_at"])
    return counts


# -- reading ---------------------------------------------------------------------------------------------------

#: Items `outline_section` lists at most.
OUTLINE_ITEMS = 300


def outline_section(request, entry, run, decision) -> Optional[dict]:
    """The reference list for `get_outline` (#176): each item's address, page and number; title and DOI from
    the `snippet` tier; the cited publication when the caller may see it. None when the run has no list."""
    from apps.api.filters.entries import EntryFilter
    from apps.content.access import Tier
    from apps.core.models import Entry
    from apps.content.citation import cite_url
    from apps.mcp import uris

    rows = list(
        run.bibitems.order_by("seq").only("id", "seq", "number", "page", "csl", "doi", "entry_id", "locator_key")[
            : OUTLINE_ITEMS + 1
        ]
    )
    if not rows:
        return None
    readable = decision.allows(Tier.SNIPPET)
    # Matching is catalog-wide: an item may cite a publication of a catalog this caller cannot see.
    cited = {row.entry_id for row in rows if row.entry_id}
    visible = (
        set(
            EntryFilter({}, queryset=Entry.objects.filter(pk__in=cited), request=request).qs.values_list(
                "pk", flat=True
            )
        )
        if cited
        else set()
    )
    items = []
    for row in rows[:OUTLINE_ITEMS]:
        item = {"page": row.page, "number": row.number}
        if row.locator_key:
            item["uri"] = uris.locator_uri(entry.pk, row.locator_key)
            item["url"] = cite_url(entry.pk, row.locator_key)
        if readable:
            item["title"] = row.csl.get("title")
            item["doi"] = row.doi
        if row.entry_id in visible:
            item["cites_entry_id"] = str(row.entry_id)
        items.append({key: value for key, value in item.items() if value is not None})
    return {"count": run.bibitems.count(), "truncated": len(rows) > OUTLINE_ITEMS, "items": items}


def reference_payload(request, bibitem, readable: bool) -> dict:
    """What a citation of a bibitem locator adds (#176): the cited work, and the library's copy if there is one."""
    payload = {"number": bibitem.number, "doi": bibitem.doi, "isbn": bibitem.isbn}
    if readable:
        payload["raw"] = bibitem.raw
        payload["csl"] = bibitem.csl
    if bibitem.entry_id:
        from apps.api.filters.entries import EntryFilter
        from apps.core.models import Entry
        from apps.mcp import uris

        cited = EntryFilter({}, queryset=Entry.objects.filter(pk=bibitem.entry_id), request=request).qs.first()
        if cited is not None:
            payload["in_library"] = {"entry_id": str(cited.pk), "title": cited.title, "uri": uris.entry_uri(cited.pk)}
    return {key: value for key, value in payload.items() if value is not None}


def citations_of(request, entry_id) -> dict:
    """What cites `entry_id` in the library, and what it cites, among the publications the caller may see."""
    from apps.api.filters.entries import EntryFilter
    from apps.content.citation import cite_url
    from apps.core.models import Entry
    from apps.extraction.models import Bibitem
    from apps.mcp import uris

    citing = list(
        Bibitem.objects.filter(entry_id=entry_id, run__is_active=True)
        .select_related("run__acquisition")
        .only("id", "page", "locator_key", "run__acquisition__entry_id")
        .order_by("run_id", "seq")
    )
    cited = list(
        Bibitem.objects.filter(run__is_active=True, run__acquisition__entry_id=entry_id, entry__isnull=False)
        .values_list("entry_id", flat=True)
        .distinct()
    )
    ids = {row.run.acquisition.entry_id for row in citing} | set(cited)
    visible = {
        entry.pk: entry
        for entry in EntryFilter({}, queryset=Entry.objects.filter(pk__in=ids), request=request).qs.only("id", "title")
    }
    cited_by = []
    for row in citing:
        source = visible.get(row.run.acquisition.entry_id)
        if source is None:
            continue
        item = {"entry_id": str(source.pk), "title": source.title, "page": row.page}
        if row.locator_key:
            item["uri"] = uris.locator_uri(source.pk, row.locator_key)
            item["url"] = cite_url(source.pk, row.locator_key)
        cited_by.append(item)
    cites = [
        {"entry_id": str(visible[pk].pk), "title": visible[pk].title, "uri": uris.entry_uri(pk)}
        for pk in cited
        if pk in visible
    ]
    return {"entry_id": str(entry_id), "cited_by": cited_by, "cites": cites}


__all__ = [
    "Block",
    "Item",
    "blocks",
    "build",
    "citations_of",
    "enrich_run",
    "is_reference_section",
    "outline_section",
    "reference_payload",
    "split",
]

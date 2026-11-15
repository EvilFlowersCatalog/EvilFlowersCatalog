"""A publication's outline from its paragraphs' section headings (IP-030 §4).

Only 2 of 435 publications have a table of contents, but 99.5 % of paragraphs carry the heading they fall under
(`Paragraph.section`). The outline is the run's distinct headings in reading order, each with its pages and
paragraph count, computed in SQL. A heading used in two places ("Úvod" in every chapter) is one section spanning
both; the paragraphs of a section are always read in `seq` order.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from django.db import connection

#: Sections listed for the model at most (one line each).
MAX_SECTIONS = 60


@dataclass
class Section:
    title: str
    first_page: int
    last_page: int
    paragraphs: int
    first_seq: int


def outline(run) -> List[Section]:
    if run is None:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT section, MIN(page), MAX(page), COUNT(*), MIN(seq) FROM extraction_paragraph "
            "WHERE run_id = %s AND section IS NOT NULL AND section <> '' GROUP BY section ORDER BY MIN(seq)",
            [run.pk],
        )
        return [Section(*row) for row in cursor.fetchall()]


def find(sections: List[Section], wanted: Optional[str]) -> Optional[Section]:
    """The section named `wanted`: exact, then case- and accent-insensitive, then by prefix or substring."""
    from apps.assistant.citations import fold

    if not sections:
        return None
    if not wanted:
        return sections[0]
    for section in sections:
        if section.title == wanted:
            return section
    folded = " ".join(fold(wanted).split())
    for section in sections:
        if " ".join(fold(section.title).split()) == folded:
            return section
    for section in sections:
        title = " ".join(fold(section.title).split())
        if title.startswith(folded) or folded in title:
            return section
    return None


def following(sections: List[Section], current: Section) -> Optional[Section]:
    for index, section in enumerate(sections):
        if section.title == current.title:
            return sections[index + 1] if index + 1 < len(sections) else None
    return None


#: Headings of front and back matter (folded: lower case, no accents), Slovak and English. The whole heading must
#: be one of these, so "Indexing in databases" or "References and pointers" stay content.
_MATTER = re.compile(
    r"^(\d+(\.\d+)*\.?\s+)?(copyright( notice)?|contents|table of contents|obsah|preface|predhovor|predslov|"
    r"foreword|uvodne slovo|acknowledg\w*|podakovanie|dedication|venovanie|about the authors?|"
    r"about the reviewers?|o autorovi|o autoroch|index|register|vecny register|menny register|references|"
    r"literatura|pouzita literatura|zoznam (pouzitej )?literatury|bibliography|bibliografia|"
    r"other books you may enjoy|why subscribe\??|share your thoughts|download a free pdf copy of this book|"
    r"title page|titulna strana|tiraz|imprint|list of (figures|tables)|zoznam (obrazkov|tabuliek|skratiek))$",
)

#: A section with fewer paragraphs than this is not where a quiz starts when none was named.
MIN_CONTENT_PARAGRAPHS = 2


def _folded(title: str) -> str:
    from apps.assistant.citations import fold

    return " ".join(fold(title or "").split()).strip(" .:-–")


def is_matter(section: Section) -> bool:
    """Front or back matter: copyright, contents, preface, acknowledgements, index, references (#166)."""
    return bool(_MATTER.match(_folded(section.title)))


def first_content(sections: List[Section]) -> Optional[Section]:
    """The first section worth studying when none was named: no front or back matter, not a stub (#166).

    Where headings are numbered ("Chapter 1", "1 Introduction"), the first numbered one: front matter has
    subheadings no list of names catches ("Who this book is for", "Get in touch"), but it is never numbered.
    """
    for section in sections:
        if level(section) is not None and not is_matter(section):
            return section
    for section in sections:
        if not is_matter(section) and section.paragraphs >= MIN_CONTENT_PARAGRAPHS:
            return section
    for section in sections:
        if not is_matter(section):
            return section
    return sections[0] if sections else None


_CHAPTER = re.compile(r"^(chapter|kapitola|part|cast|lesson|lekcia)\s+[0-9ivxlc]+\b")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)(?:[.):]|\s)")


def level(section: Section) -> Optional[int]:
    """How deep a heading is, from its numbering: "Chapter 11" and "11 …" are 1, "11.2" is 2; None unnumbered.

    Headings carry no level of their own (`Paragraph.section` is the text), so the numbering is all there is.
    """
    title = _folded(section.title)
    if _CHAPTER.match(title):
        return 1
    match = _NUMBERED.match(title)
    if match:
        return match.group(1).count(".") + 1
    return None


#: Sections one chapter's span takes at most.
MAX_SPAN = 40


def span(sections: List[Section], start: Section) -> List[Section]:
    """`start` and the sections under it (#166): until the next heading of its level or higher, or back matter.

    A chapter heading ("Chapter 11: Common Design Patterns") owns only its intro paragraphs; its patterns are
    headings of their own, numbered deeper or not at all. An unnumbered heading spans only itself.
    """
    index = next((i for i, section in enumerate(sections) if section.title == start.title), None)
    if index is None:
        return [start]
    depth = level(start)
    if depth is None:
        return [start]
    result = [start]
    for section in sections[index + 1 :]:
        other = level(section)
        if (other is not None and other <= depth) or is_matter(section) or len(result) >= MAX_SPAN:
            break
        result.append(section)
    return result


def end_seq(sections: List[Section], last: Section) -> Optional[int]:
    """Where the paragraphs of `last` end: the first paragraph of the section after it; None at the end.

    A heading used twice ("Úvod" in every chapter) is one outline entry starting at its first use; bounded by the
    next heading, its paragraphs are those of that first use only, not of every chapter that repeats it.
    """
    upcoming = following(sections, last)
    return upcoming.first_seq if upcoming is not None else None

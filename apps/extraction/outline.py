"""A publication's outline from its paragraphs' section headings (IP-030 §4).

Only 2 of 435 publications have a table of contents, but 99.5 % of paragraphs carry the heading they fall under
(`Paragraph.section`). The outline is the run's distinct headings in reading order, each with its pages and
paragraph count, computed in SQL. A heading used in two places ("Úvod" in every chapter) is one section spanning
both; the paragraphs of a section are always read in `seq` order.
"""

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

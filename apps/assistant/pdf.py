"""The publication a reader has open, as text the explain agent can read.

Page numbers here are the reader's page numbers — the ones the viewer shows —
not the source file's. The download a reader gets is cut down to their
acquisition's page range and, for PDFs, has the library's license page spliced
in as page 2 (`apps.core.modifiers.pdf.PDFModifier`). A model told "page 14"
must read what the reader sees on page 14, so the same numbering is rebuilt
here over the original file.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import fitz
from django.conf import settings
from django.core.cache import cache

from apps.core.fields.multirange import depack
from apps.core.models import UserAcquisition

logger = logging.getLogger(__name__)

#: How long extracted page text is kept. A book is extracted once and then read
#: from the cache by every explain request about it for this long.
CACHE_TIMEOUT = 60 * 60

#: Stands in for the license page, which has no publication text on it.
LICENSE_PAGE_TEXT = "(This page is the library's license notice; it is not part of the publication.)"

#: Characters of context kept around each search hit.
SNIPPET_RADIUS = 200

_WORD = re.compile(r"\w+", re.UNICODE)


class DocumentUnavailable(Exception):
    """The acquisition has no readable PDF behind it."""


@dataclass
class Heading:
    level: int
    title: str
    page: int


class ReaderDocument:
    def __init__(self, pages: List[str], toc: List[Heading]):
        self.pages = pages
        self.toc = toc

    @classmethod
    def for_user_acquisition(cls, user_acquisition: UserAcquisition) -> "ReaderDocument":
        acquisition = user_acquisition.acquisition
        if acquisition.mime != "application/pdf" or not acquisition.content:
            raise DocumentUnavailable(acquisition.mime)

        key = f"assistant:pdf:{acquisition.pk}:{user_acquisition.range or ''}"
        cached = cache.get(key)
        if cached is None:
            cached = cls._extract(acquisition, user_acquisition.range)
            cache.set(key, cached, CACHE_TIMEOUT)

        pages, toc = cached
        return cls(pages, [Heading(*heading) for heading in toc])

    @staticmethod
    def _extract(acquisition, page_range: Optional[str]):
        try:
            with acquisition.content.open("rb") as file:
                document = fitz.open(stream=file.read(), filetype="pdf")
        except Exception as error:
            logger.exception("Could not open acquisition %s for the assistant", acquisition.pk)
            raise DocumentUnavailable(str(error)) from error

        with document:
            # 1-based source page numbers, in the order the reader sees them.
            source_pages = [page for page in (depack(page_range) if page_range else []) if 1 <= page <= len(document)]
            if not source_pages:
                source_pages = list(range(1, len(document) + 1))

            pages = [document[page - 1].get_text("text").strip() for page in source_pages]
            reader_page = {source: index + 1 for index, source in enumerate(source_pages)}

            # Mirrors `PDFModifier.generate`, which inserts the license page at
            # index 1 of every PDF it serves.
            licensed = acquisition.mime in settings.EVILFLOWERS_MODIFIERS
            if licensed:
                pages.insert(1, LICENSE_PAGE_TEXT)
                reader_page = {source: page + 1 if page > 1 else page for source, page in reader_page.items()}

            toc = [
                (level, title.strip(), reader_page[page])
                for level, title, page, *_rest in document.get_toc(simple=True)
                if page in reader_page and title.strip()
            ]

        return pages, toc

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, number: int) -> Optional[str]:
        if 1 <= number <= self.page_count:
            return self.pages[number - 1]
        return None

    def section_of(self, number: int) -> List[str]:
        """Headings enclosing a page, outermost first (book part → chapter → section)."""
        trail: Dict[int, str] = {}
        for heading in self.toc:
            if heading.page > number:
                break
            trail[heading.level] = heading.title
            # A new heading closes every deeper one that came before it.
            for level in [level for level in trail if level > heading.level]:
                del trail[level]
        return [trail[level] for level in sorted(trail)]

    def outline(self) -> List[dict]:
        return [{"level": heading.level, "title": heading.title, "page": heading.page} for heading in self.toc]

    def search(self, query: str, limit: int = 8) -> List[dict]:
        """Pages ranked by how many of the query's words they contain.

        Plain term overlap, no index: a book is a few hundred pages of text
        already in memory, and the model reads the hits itself. An exact phrase
        match ranks above any scattered one.
        """
        terms = {term.lower() for term in _WORD.findall(query) if len(term) > 2}
        phrase = " ".join(query.lower().split())
        if not terms and not phrase:
            return []

        hits = []
        for index, text in enumerate(self.pages):
            lowered = " ".join(text.lower().split())
            words = set(_WORD.findall(lowered))
            score = sum(1 for term in terms if term in words)
            position = lowered.find(phrase) if phrase else -1
            if position >= 0:
                score += len(terms) + 1
            if score == 0:
                continue

            if position < 0:
                position = min((lowered.find(term) for term in terms if term in words), default=0)
            start = max(position - SNIPPET_RADIUS, 0)
            snippet = lowered[start : position + len(phrase) + SNIPPET_RADIUS]
            hits.append({"page": index + 1, "score": score, "snippet": snippet})

        hits.sort(key=lambda hit: (-hit["score"], hit["page"]))
        return hits[:limit]

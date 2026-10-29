"""Find the agent's quotes on the pages of the original PDF (IP-018 Quote resolution).

Ported from monad-knowledge `monad_knowledge/reading/highlights.py` (`_locate`,
`_page_index`), which does the same for claim evidence sentences. Two steps:

1. `quote_in_text` — the gate at answer time: a quote is kept only when it
   occurs in the page text the agent read, whitespace and hyphenated line
   breaks aside. Paraphrases stop here.
2. `QuoteResolver.locate` — positions: PyMuPDF searches the quote on the
   page the agent read it on, then on the rest of the reader's range
   (`exact`); failing that, six-word windows of it on the page that holds most
   of them (`partial`); otherwise it is `missed`.

Positions are page fractions with a top-left origin, one box per text line.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import fitz

from apps.assistant.pdf import ReaderDocument

logger = logging.getLogger(__name__)

# Constants as in monad-knowledge.
MIN_SPAN_CHARS = 20
WINDOW_WORDS = 6
MIN_WINDOW_CHARS = 20
MAX_WINDOW_QUADS = 3
MIN_WINDOW_SHARE = 0.5

_WS = re.compile(r"\s+")
#: A word broken over a line: `exam-\nple`. PyMuPDF's TEXT_DEHYPHENATE joins
#: these in the PDF step, so the quote is joined the same way.
_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")


def collapse(text: str) -> str:
    """Hyphenated line breaks joined, whitespace collapsed; case kept for `search_for`."""
    return _WS.sub(" ", _HYPHEN_BREAK.sub(r"\1\2", text or "")).strip()


def _norm(text: str) -> str:
    return collapse(text).lower()


def quote_in_text(quote: str, page_text: str) -> bool:
    needle = _norm(quote)
    return bool(needle) and needle in _norm(page_text)


@dataclass
class Located:
    #: `exact`, `partial` or `missed`.
    match: str
    #: Reader page the quote was found on; None when missed.
    page: Optional[int] = None
    #: `{x0, y0, x1, y1}` page fractions, one per text line.
    quads: List[dict] = field(default_factory=list)
    #: Page size in PDF points, for the item's SVG.
    size: Optional[tuple] = None


class QuoteResolver:
    """Text pages of the reader's range of one PDF, built once and searched per quote."""

    FLAGS = fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP

    def __init__(self, pdf: "fitz.Document", document: ReaderDocument):
        self.pdf = pdf
        self.document = document
        #: Reader page → (fitz page, text page, normalised text), lazily.
        self._pages = {}

    @classmethod
    def open(cls, document: ReaderDocument) -> "QuoteResolver":
        with document.acquisition.content.open("rb") as file:
            pdf = fitz.open(stream=file.read(), filetype="pdf")
        return cls(pdf, document)

    def close(self):
        self.pdf.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _page(self, reader_page: int):
        if reader_page not in self._pages:
            source = self.document.source_page(reader_page)
            if source is None or not 1 <= source <= len(self.pdf):
                self._pages[reader_page] = None
            else:
                page = self.pdf[source - 1]
                textpage = page.get_textpage(flags=self.FLAGS)
                self._pages[reader_page] = (page, textpage, _norm(textpage.extractText()))
        return self._pages[reader_page]

    def _order(self, preferred: Optional[int]) -> List[int]:
        """Reader pages to search: the agent's page first, a cheap first guess, then the rest."""
        pages = list(range(1, self.document.page_count + 1))
        if preferred in pages:
            pages.remove(preferred)
            pages.insert(0, preferred)
        return pages

    def locate(self, quote: str, page: Optional[int] = None) -> Located:
        needle = collapse(quote)
        if len(needle) < MIN_SPAN_CHARS:
            return Located("missed")
        low = needle.lower()
        order = self._order(page)

        for number in order:
            entry = self._page(number)
            if entry is None or low not in entry[2]:
                continue
            fitz_page, textpage, _text = entry
            hits = fitz_page.search_for(needle, quads=True, textpage=textpage)
            if hits:
                return self._located("exact", number, fitz_page, hits)

        words = needle.split()
        windows = [" ".join(words[k : k + WINDOW_WORDS]) for k in range(0, len(words), WINDOW_WORDS)]
        windows = [window for window in windows if len(window) >= MIN_WINDOW_CHARS]
        if not windows:
            return Located("missed")

        best_page, best_found = None, []
        for number in order:
            entry = self._page(number)
            if entry is None:
                continue
            found = [window for window in windows if window.lower() in entry[2]]
            if len(found) > len(best_found):
                best_page, best_found = number, found
        if best_page is None or len(best_found) / len(windows) < MIN_WINDOW_SHARE:
            return Located("missed")

        fitz_page, textpage, _text = self._page(best_page)
        hits = []
        for window in best_found:
            window_hits = fitz_page.search_for(window, quads=True, textpage=textpage)
            if 0 < len(window_hits) <= MAX_WINDOW_QUADS:
                hits.extend(window_hits)
        if not hits:
            return Located("missed")
        return self._located("partial", best_page, fitz_page, hits)

    @staticmethod
    def _located(match: str, number: int, fitz_page, hits) -> Located:
        bounds = fitz_page.rect
        width, height = bounds.width or 1, bounds.height or 1
        quads = []
        for hit in hits:
            rect = hit.rect
            quads.append(
                {
                    "x0": round(max((rect.x0 - bounds.x0) / width, 0), 5),
                    "y0": round(max((rect.y0 - bounds.y0) / height, 0), 5),
                    "x1": round(min((rect.x1 - bounds.x0) / width, 1), 5),
                    "y1": round(min((rect.y1 - bounds.y0) / height, 1), 5),
                }
            )
        return Located(match, number, quads, (bounds.width, bounds.height))


def svg(quads: List[dict], size: tuple) -> str:
    """The item's `content`: its line boxes in PDF points of the page (IP-018 Q7)."""
    width, height = size
    rects = "".join(
        f'<rect x="{q["x0"] * width:.2f}" y="{q["y0"] * height:.2f}" '
        f'width="{(q["x1"] - q["x0"]) * width:.2f}" height="{(q["y1"] - q["y0"]) * height:.2f}"/>'
        for q in quads
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}">'
        f'<g fill="#facc15" fill-opacity="0.35">{rects}</g></svg>'
    )

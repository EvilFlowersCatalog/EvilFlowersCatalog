"""What the assistant's IP-030 tools share: a publication's readable text, in the right page numbers.

`Text.of(context, entry)` is the extraction run of a publication the model may read, and how its pages are
numbered for the reader:

- the publication of a chat bound to the reader's copy reads that copy's run, with the viewer's page numbers
  (`ReaderExtraction`);
- any other publication reads its active run with the original file's pages (as the viewer shows an
  open-access publication), only where `discovery._may_read_text` allows: never Readium LCP, never outside the
  entry's IP ranges.

`ToolContext` is what a tool call needs besides its arguments: the chat, the reader's copy when bound, the
ledger, the page budgets and the reader's language.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from apps.mcp.errors import ToolError

#: Characters of a passage shown to the model (as `search_library_text`).
PASSAGE_CHARS = 600

_UNREADABLE = {
    "readium": "This publication is lent through Readium LCP, so its text cannot be read here. Answer from its "
    "catalogue record, and tell the reader to borrow it to read it.",
    "ip_blocked": "This publication may only be read from the library's network.",
    "none": "This publication has not been processed into paragraphs, so this is not available for it. Use "
    "read_pdf_page instead.",
}


@dataclass
class ToolContext:
    request: object = None
    chat: object = None
    document: object = None
    ledger: object = None
    reading: object = None
    language: Optional[str] = None
    #: Pages read by study tools this turn (EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES, Q8).
    study_pages: Dict[str, List[int]] = field(default_factory=dict)
    #: Frames a tool asks the turn to send (`study`, `evidence`), as `(event, data)`.
    frames: List[tuple] = field(default_factory=list)
    #: Model time spent by tools' own requests (study, section summary, vision), booked on the tool row (Q2).
    model_seconds: float = 0.0

    def book(self, delta) -> None:
        if getattr(delta, "done", False):
            self.model_seconds += getattr(delta, "model_seconds", 0.0) or 0.0

    @property
    def bound_entry_id(self) -> Optional[str]:
        acquisition = getattr(self.document, "acquisition", None)
        return str(acquisition.entry_id) if acquisition is not None else None


@dataclass
class Text:
    entry: object
    run: object
    page_of: Callable[[int], Optional[int]]
    bound: bool = False

    @classmethod
    def of(cls, context: ToolContext, entry) -> "Text":
        from apps.extraction.discovery import _entry_run, _may_read_text

        if entry.read_config("readium_enabled"):
            raise ToolError(_UNREADABLE["readium"])
        if context.document is not None and context.bound_entry_id == str(entry.pk):
            from apps.extraction.reader import ReaderExtraction

            extraction = ReaderExtraction.for_document(context.document)
            if extraction is None:
                raise ToolError(_UNREADABLE["none"])
            return cls(entry, extraction.run, extraction.reader_page, bound=True)
        if not _may_read_text(context.request, entry):
            raise ToolError(_UNREADABLE["ip_blocked"])
        run = _entry_run(entry.pk)
        if run is None:
            raise ToolError(_UNREADABLE["none"])
        return cls(entry, run, lambda page: page)

    def passage(self, paragraph, chars: int = PASSAGE_CHARS) -> Optional[dict]:
        """A paragraph as a labelled-to-be passage; None when its page is outside the reader's copy."""
        page = self.page_of(paragraph.page)
        if page is None:
            return None
        text = paragraph.text if len(paragraph.text) <= chars else paragraph.text[:chars].rsplit(" ", 1)[0] + " […]"
        return {
            "kind": "passage",
            "entry_id": str(self.entry.pk),
            "title": self.entry.title,
            "page": page,
            "source_page": paragraph.page if page != paragraph.page else None,
            "section": paragraph.section,
            "paragraph_id": str(paragraph.pk),
            "text": text,
        }

    def figure(self, region) -> Optional[dict]:
        page = self.page_of(region.page)
        if page is None:
            return None
        return {
            "kind": region.kind,
            "entry_id": str(self.entry.pk),
            "title": self.entry.title,
            "page": page,
            "source_page": region.page if page != region.page else None,
            "region_id": str(region.pk),
            "caption": (region.printed_caption or region.caption or region.description or "")[:300] or None,
        }


def entry_for(context: ToolContext, entry_id) -> object:
    """The publication named by the model (or the chat's), if this reader may see it; `ToolError` otherwise."""
    from apps.assistant.tools import _publication

    if entry_id:
        return _publication(context.request, entry_id)
    if context.bound_entry_id:
        return _publication(context.request, context.bound_entry_id)
    chat_entry = getattr(context.chat, "entry_id", None)
    if chat_entry:
        return _publication(context.request, chat_entry)
    raise ToolError("`entry_id` is required: the publication's id from a tool result.")


#: What a ledger source keeps of a passage or figure (`evidence` module docstring).
SOURCE_KEYS = ("kind", "entry_id", "title", "page", "source_page", "section", "paragraph_id", "region_id")


def label_units(ledger, units: List[dict]) -> Dict[str, dict]:
    """Label passages (`E#`) and figures (`F#`) of a flat list, in place; returns what was issued."""
    issued: Dict[str, dict] = {}
    for unit in units:
        source = {key: unit.get(key) for key in SOURCE_KEYS}
        label, _new = ledger.issue(source)
        unit["label"] = label
        text = unit.get("text") or unit.get("caption")
        if text:
            ledger.texts[label] = text
        issued[label] = ledger.sources[label]
    return issued

"""What the assistant's IP-030 tools share: a publication's readable text, in the right page numbers.

`Text.of(context, entry)` is the extraction run of a publication the model may read, and how its pages are
numbered for the reader:

- the publication of a chat bound to the reader's copy reads that copy's run, with the viewer's page numbers
  (`ReaderExtraction`);
- any other publication reads its active run with the original file's pages (as the viewer shows an
  open-access publication), only where the content gate allows a passage (`apps.content.access`; under
  `parity`: never Readium LCP, never outside the entry's IP ranges).

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
    "policy": "The library's content policy does not let this publication's text be read here. Answer from its "
    "catalogue record.",
}
#: The content gate's reason (`apps.content.access.Reason`) → the message above.
_UNREADABLE_BY_REASON = {"lcp": "readium", "outside_ip": "ip_blocked"}


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
    #: The gate's decision when the reader's basis is metered (#175): each passage handed out is charged.
    decision: object = None
    request: object = None

    def __post_init__(self):
        self._budget_left = self.decision.budget_left if self.decision is not None else None

    @classmethod
    def of(cls, context: ToolContext, entry) -> "Text":
        from apps.content import access
        from apps.extraction.discovery import _entry_run

        decision = access.decide(context.request, entry, surface=access.Surface.ASSISTANT, visible=True)
        if decision.reason == access.Reason.LCP and not decision.allows(access.Tier.PASSAGE):
            raise ToolError(_UNREADABLE["readium"])
        if context.document is not None and context.bound_entry_id == str(entry.pk):
            from apps.extraction.reader import ReaderExtraction

            extraction = ReaderExtraction.for_document(context.document)
            if extraction is None:
                raise ToolError(_UNREADABLE["none"])
            return cls(entry, extraction.run, extraction.reader_page, bound=True)
        if not decision.allows(access.Tier.PASSAGE):
            raise ToolError(_UNREADABLE[_UNREADABLE_BY_REASON.get(decision.reason, "policy")])
        run = _entry_run(entry.pk)
        if run is None:
            raise ToolError(_UNREADABLE["none"])
        metered = decision if decision.metered else None
        return cls(entry, run, lambda page: page, decision=metered, request=context.request)

    def passage(self, paragraph, chars: int = PASSAGE_CHARS) -> Optional[dict]:
        """A paragraph as a labelled-to-be passage; None when its page is outside the reader's copy.

        Under a metered basis (#175) the passage is charged to the reader's budget. A passage is never cut to fit:
        one that does not fit in what is left is withheld (None), and once the budget is spent the next tool
        call's decision drops to `snippet` and refuses.
        """
        page = self.page_of(paragraph.page)
        if page is None:
            return None
        text = paragraph.text if len(paragraph.text) <= chars else paragraph.text[:chars].rsplit(" ", 1)[0] + " […]"
        if self.decision is not None:
            from apps.content import disclosure

            charged = disclosure.record(
                self.request,
                self.decision,
                [disclosure.Item(chars=len(text), locator=getattr(paragraph, "locator_key", None))],
                budget_left=self._budget_left,
            )
            self._budget_left = charged.budget_left
            if charged.left_out:
                return None
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


#: What a ledger source keeps of a passage or figure (`evidence` module docstring). `language` only when it is not
#: the reader's (#157), so a citation says the original is in another language.
SOURCE_KEYS = (
    "kind",
    "entry_id",
    "title",
    "page",
    "source_page",
    "section",
    "paragraph_id",
    "region_id",
    "language",
)


def label_units(ledger, units: List[dict]) -> Dict[str, dict]:
    """Label passages (`E#`) and figures (`F#`) of a flat list, in place; returns what was issued."""
    from apps.assistant.evidence import attach_locators

    issued: Dict[str, dict] = {}
    sources = [{key: unit.get(key) for key in SOURCE_KEYS} for unit in units]
    attach_locators(sources)  # one query for the batch instead of one per new label (#171)
    for unit, source in zip(units, sources):
        label, _new = ledger.issue(source)
        unit["label"] = label
        text = unit.get("text") or unit.get("caption")
        if text:
            ledger.texts[label] = text
        issued[label] = ledger.sources[label]
    return issued

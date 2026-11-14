"""Citations for the reader's viewer, resolved while the answer streams (IP-030 §2).

A label in the ledger is kept small: publication, page, section, and the paragraph or region it points at.
`complete` turns it into what the portal needs to land on the evidence and show it:

- a passage: the paragraph's `bbox` on the page and the quads of its **best sentence** — the sentence of the
  paragraph sharing the most words with the answer's sentence that cites it (accents and case folded; the first
  on a tie). This is kotaemon's "highlight the evidence unit" without asking a CPU model to copy quotes;
- a figure, table or equation: its printed caption, its `bbox` and the crop's URL.

`quote`, `caption` and `image_url` are present only where the reader may read the publication's text outside
the viewer (`discovery._may_read_text`): never for Readium LCP, never outside the entry's IP ranges. `readable`
says which; an unreadable passage still resolves to its page.

`Resolver` follows Onyx's streaming citation processor: it watches the chunk stream for `[E3]`, `[E3, F2]`, holds
back a partial `[E` until it is complete (so a chip never flickers half-written) and reports each label the first
time it appears. What it lets through, concatenated, is exactly the text the model wrote.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from apps.assistant.evidence import REGION_KINDS, Ledger, citation, citing_sentence

#: Characters of a passage's quote.
QUOTE_CHARS = 240

#: A tail that may still become a citation: `[`, `[E`, `[E1`, `[E1, ` … (case-insensitive).
_PARTIAL = re.compile(r"\[\s*(?:[EF]\d*(?:\s*[,;]\s*(?:[EF]\d*)?)*)?$", re.IGNORECASE)
#: Longest tail held back; anything longer is not a label.
MAX_HELD = 48

_GROUP = re.compile(r"\[\s*([EF]\d+(?:\s*[,;]\s*[EF]\d+)*)\s*\]", re.IGNORECASE)
_LABEL = re.compile(r"([EF])(\d+)", re.IGNORECASE)
_WORD = re.compile(r"\w+", re.UNICODE)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _words(text: str) -> set:
    return {word for word in _WORD.findall(fold(text)) if len(word) > 2}


def best_sentence(sentences: List, citing: str) -> Optional[object]:
    """The sentence with the most words in common with `citing`; the first on a tie, None without any."""
    if not sentences:
        return None
    wanted = _words(citing)
    best, best_score = sentences[0], -1
    for sentence in sentences:
        score = len(wanted & _words(sentence.text))
        if score > best_score:
            best, best_score = sentence, score
    return best


class _Access:
    """Per-request memo: an entry's readability and title."""

    def __init__(self, request):
        self.request = request
        self._readable: Dict[str, bool] = {}

    def readable(self, entry_id) -> bool:
        from apps.core.models import Entry
        from apps.extraction.discovery import _may_read_text

        key = str(entry_id)
        if key not in self._readable:
            entry = Entry.objects.filter(pk=entry_id).first() if entry_id else None
            self._readable[key] = bool(entry is not None and _may_read_text(self.request, entry))
        return self._readable[key]


def _access(request) -> _Access:
    access = getattr(request, "_evilflowers_citation_access", None)
    if access is None:
        access = _Access(request)
        try:
            request._evilflowers_citation_access = access
        except AttributeError:
            pass
    return access


def complete(label: str, source: dict, sentence: str, request) -> dict:
    """The citation of `label` for the viewer (module docstring)."""
    result = citation(label, source)
    readable = _access(request).readable(source.get("entry_id"))
    result["readable"] = readable
    kind = source.get("kind")
    if kind in REGION_KINDS or source.get("region_id"):
        result.update(_region(source, readable, request))
    elif source.get("paragraph_id") and not str(source["paragraph_id"]).startswith("page:"):
        result.update(_passage(source, readable, sentence))
    return result


def _passage(source: dict, readable: bool, sentence: str) -> dict:
    from apps.extraction.models import Paragraph

    paragraph = Paragraph.objects.filter(pk=source["paragraph_id"]).first()
    if paragraph is None:
        # A newer extraction run replaced it: the page is still right.
        return {}
    extra = {"bbox": paragraph.bbox}
    chosen = best_sentence(list(paragraph.sentences.order_by("seq")), sentence)
    # A bound chat speaks the reader's page numbers; the sentence is on a page of the original file.
    offset = int(source.get("page") or paragraph.page) - int(source.get("source_page") or paragraph.page)
    if chosen is not None:
        extra["quads"] = chosen.quads
        extra["sentence_page"] = chosen.page + offset
        if readable:
            extra["quote"] = _clip(chosen.text)
    elif readable:
        extra["quote"] = _clip(paragraph.text)
    return extra


def _region(source: dict, readable: bool, request) -> dict:
    from django.urls import reverse

    from apps.extraction.models import Region

    region = Region.objects.filter(pk=source["region_id"]).select_related("run__acquisition").first()
    if region is None:
        return {}
    extra = {"bbox": region.bbox}
    if readable:
        caption = region.printed_caption or region.caption or region.description
        if caption:
            extra["caption"] = _clip(caption)
        if region.image_path:
            entry_id = source.get("entry_id") or region.run.acquisition.entry_id
            try:
                extra["image_url"] = request.build_absolute_uri(
                    reverse("files:entry-region-image", kwargs={"entry_id": entry_id, "region_id": region.pk})
                )
            except Exception:
                pass
    return extra


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1].rsplit(" ", 1)[0] + "…"


class Resolver:
    """Streaming citation resolution over the chunks of one model round (module docstring)."""

    def __init__(self, ledger: Ledger, request=None, seen: Optional[set] = None):
        self.ledger = ledger
        self.request = request
        #: Labels already reported in this turn (a later round does not report them again).
        self.seen = seen if seen is not None else set()
        self.text = ""
        self._held = ""
        self._scanned = 0

    def feed(self, chunk: str) -> Tuple[str, List[dict]]:
        """`(text to pass on now, citations that just became complete)`."""
        buffer = self._held + (chunk or "")
        self._held = ""
        start = buffer.rfind("[")
        if start != -1 and "]" not in buffer[start:] and len(buffer) - start <= MAX_HELD:
            if _PARTIAL.fullmatch(buffer[start:]):
                self._held = buffer[start:]
                buffer = buffer[:start]
        return self._emit(buffer)

    def finish(self) -> Tuple[str, List[dict]]:
        """Whatever was held back, and its citations; the stream has ended."""
        buffer, self._held = self._held, ""
        return self._emit(buffer)

    def _emit(self, buffer: str) -> Tuple[str, List[dict]]:
        self.text += buffer
        found: List[dict] = []
        # Re-scan from a little before the last scan: a group closes only when its `]` arrives.
        for group in _GROUP.finditer(self.text, max(0, self._scanned - MAX_HELD)):
            if group.end() <= self._scanned:
                continue
            sentence = citing_sentence(self.text, group.start())
            for match in _LABEL.finditer(group.group(1)):
                label = f"{match.group(1).upper()}{int(match.group(2))}"
                if label in self.seen or label not in self.ledger.sources:
                    continue
                self.seen.add(label)
                source = self.ledger.sources[label]
                if self.request is not None:
                    found.append(complete(label, source, sentence, self.request))
                else:
                    found.append(citation(label, source))
        self._scanned = len(self.text)
        return buffer, found

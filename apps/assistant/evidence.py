"""Evidence labels: passages the model may cite, and the citations in its answer (IP-024 §1).

A library passage shown to the model carries a short label (`E1`, `E2`, …). The model cites the label right after
the sentence it supports, and the closing answer's labels are resolved back to a publication and page here.

Labels are unique per chat, not per turn: tool results are replayed verbatim on every later round and turn, so
two searches that both started at `E1` would show the model two different `E1` passages. The ledger is rebuilt
from the chat's own tool rows at the start of each turn — each labelling row keeps `evidence: {label: source}`
in its full payload (`ChatMessage.tool_result`), which the model never sees.
"""

import re
from typing import Dict, List, Optional, Tuple

#: Key of the label → source map in a tool's full payload.
EVIDENCE_KEY = "evidence"

#: `[E3]`, `[E3, E5]`, `[E3; E5]`; `[E3][E5]` is two matches.
_CITATION = re.compile(r"\[\s*(E\d+(?:\s*[,;]\s*E\d+)*)\s*\]", re.IGNORECASE)
_LABEL = re.compile(r"E(\d+)", re.IGNORECASE)


class Ledger:
    """Labels issued in one chat, and what each one points at."""

    def __init__(self, sources: Optional[Dict[str, dict]] = None, texts: Optional[Dict[str, str]] = None):
        self.sources: Dict[str, dict] = dict(sources or {})
        #: The passage text behind each label, as the model saw it; for the support check (#117).
        self.texts: Dict[str, str] = dict(texts or {})
        self._by_paragraph: Dict[str, str] = {
            source["paragraph_id"]: label for label, source in self.sources.items() if source.get("paragraph_id")
        }

    @classmethod
    def for_chat(cls, chat) -> "Ledger":
        from apps.assistant.models import ChatMessage

        sources: Dict[str, dict] = {}
        texts: Dict[str, str] = {}
        rows = chat.messages.filter(role=ChatMessage.Role.TOOL, tool_result__has_key=EVIDENCE_KEY).order_by(
            "created_at"
        )
        for result in rows.values_list("tool_result", flat=True):
            evidence = result.get(EVIDENCE_KEY) if isinstance(result, dict) else None
            if isinstance(evidence, dict):
                sources.update(evidence)
                texts.update(_texts(result))
        return cls(sources, texts)

    def _next(self) -> str:
        numbers = [int(match.group(1)) for match in map(_LABEL.fullmatch, self.sources) if match]
        return f"E{max(numbers, default=0) + 1}"

    def issue(self, source: dict) -> Tuple[str, bool]:
        """The label for this passage, and whether it is new. A paragraph seen before keeps its label."""
        paragraph_id = source.get("paragraph_id")
        if paragraph_id and paragraph_id in self._by_paragraph:
            return self._by_paragraph[paragraph_id], False
        label = self._next()
        self.sources[label] = source
        if paragraph_id:
            self._by_paragraph[paragraph_id] = label
        return label, True

    def cite(self, text: str) -> Tuple[List[dict], List[str]]:
        """`(citations, unknown labels)` of an answer, each in order of first appearance."""
        citations: List[dict] = []
        unknown: List[str] = []
        for label in labels_in(text):
            source = self.sources.get(label)
            if source is None:
                unknown.append(label)
            else:
                citations.append({"label": label, **{k: v for k, v in source.items() if k != "paragraph_id"}})
        return citations, unknown


def labels_in(text: Optional[str]) -> List[str]:
    """Evidence labels cited in `text`, normalised (`e03` → `E3`), each once, in order of first appearance."""
    labels: List[str] = []
    for group in _CITATION.finditer(text or ""):
        for match in _LABEL.finditer(group.group(1)):
            label = f"E{int(match.group(1))}"
            if label not in labels:
                labels.append(label)
    return labels


def label_passages(payload: dict, ledger: Ledger) -> dict:
    """Label the readable passages of a `search_library_text` payload, in place.

    A passage whose text is withheld (Readium LCP) gets no label: the model has nothing in it to cite. The labels
    issued for this payload are kept under `evidence`, which `tools.slim` keeps out of the model's view.
    """
    issued: Dict[str, dict] = {}
    for item in payload.get("items") or []:
        for passage in item.get("passages") or []:
            if not passage.get("text"):
                continue
            source = {
                "entry_id": item.get("entry_id"),
                "title": item.get("title"),
                "page": passage.get("page"),
                "section": passage.get("section"),
                "paragraph_id": passage.get("paragraph_id"),
            }
            label, _new = ledger.issue({key: value for key, value in source.items() if value not in (None, "")})
            passage["label"] = label
            ledger.texts[label] = passage["text"]
            issued[label] = ledger.sources[label]
    if issued:
        payload[EVIDENCE_KEY] = issued
    return payload


def label_pages(payload: dict, ledger: Ledger) -> dict:
    """Label the pages of a plain chat's `read_pdf_page` payload, in place (#127).

    A page read from a publication the chat is not bound to has no viewer to jump to, so `[[page:N]]` would
    point nowhere; it is cited like a passage, as `[E#]` resolved to publication and page. A page read again
    keeps its label: its key stands in for a paragraph id.
    """
    issued: Dict[str, dict] = {}
    for page in payload.get("pages") or []:
        if not page.get("text"):
            continue
        source = {
            "entry_id": payload.get("entry_id"),
            "title": payload.get("title"),
            "page": page.get("page"),
            "paragraph_id": f"page:{payload.get('entry_id')}:{page.get('page')}",
        }
        label, _new = ledger.issue({key: value for key, value in source.items() if value not in (None, "")})
        page["label"] = label
        ledger.texts[label] = page["text"]
        issued[label] = ledger.sources[label]
    if issued:
        payload[EVIDENCE_KEY] = issued
    return payload


def _texts(payload: dict) -> Dict[str, str]:
    """Label → passage (or page) text of a labelled payload."""
    texts = {
        passage["label"]: passage["text"]
        for item in payload.get("items") or []
        for passage in item.get("passages") or []
        if passage.get("label") and passage.get("text")
    }
    for page in payload.get("pages") or []:
        if page.get("label") and page.get("text"):
            texts[page["label"]] = page["text"]
    return texts

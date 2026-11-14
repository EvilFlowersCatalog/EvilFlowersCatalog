"""Evidence labels: what the model may cite, and the citations in its answer (IP-024 §1, IP-030 §2).

Two label series, one ledger per chat:

- `E1`, `E2`, … a passage (a paragraph of a processed PDF) or a page read whole;
- `F1`, `F2`, … a figure, table or equation (`extraction_region`).

The model cites a label right after the sentence it supports. The answer's labels are resolved back to a
publication, page and paragraph or figure here; `apps.assistant.citations` adds what the reader's viewer needs
(the paragraph's box, the best sentence's quads, a short quote, the figure's crop), and resolves them while the
answer streams.

Labels are unique per chat, not per turn: tool results are replayed verbatim on every later round and turn, so
two searches that both started at `E1` would show the model two different `E1` passages. The ledger is rebuilt
from the chat's own tool rows at the start of each turn — each labelling row keeps `evidence: {label: source}`
in its full payload (`ChatMessage.tool_result`), which the model never sees. A source is kept small (ids, title,
page, section); the rest is looked up when the label is cited, under the access rules of that moment.
"""

import re
from typing import Dict, List, Optional, Tuple

#: Key of the label → source map in a tool's full payload.
EVIDENCE_KEY = "evidence"

#: Source kinds.
PASSAGE, PAGE = "passage", "page"
REGION_KINDS = ("figure", "table", "equation")

#: `[E3]`, `[E3, E5]`, `[E3; F2]`; `[E3][E5]` is two matches.
_CITATION = re.compile(r"\[\s*([EF]\d+(?:\s*[,;]\s*[EF]\d+)*)\s*\]", re.IGNORECASE)
_LABEL = re.compile(r"([EF])(\d+)", re.IGNORECASE)

#: Keys of a source that are bookkeeping, not part of a citation.
_PRIVATE = ("source_page",)


def _key(source: dict) -> Optional[str]:
    """What makes two sources the same: the paragraph (or page), or the region."""
    if source.get("region_id"):
        return f"region:{source['region_id']}"
    return source.get("paragraph_id")


class Ledger:
    """Labels issued in one chat, and what each one points at."""

    def __init__(self, sources: Optional[Dict[str, dict]] = None, texts: Optional[Dict[str, str]] = None):
        self.sources: Dict[str, dict] = dict(sources or {})
        #: The passage text behind each label, as the model saw it; for the support check (#117).
        self.texts: Dict[str, str] = dict(texts or {})
        self._by_key: Dict[str, str] = {}
        for label, source in self.sources.items():
            key = _key(source)
            if key:
                self._by_key[key] = label

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

    def _next(self, series: str) -> str:
        numbers = [
            int(match.group(2))
            for match in map(_LABEL.fullmatch, self.sources)
            if match and match.group(1).upper() == series
        ]
        return f"{series}{max(numbers, default=0) + 1}"

    def issue(self, source: dict) -> Tuple[str, bool]:
        """The label for this source, and whether it is new. A paragraph or figure seen before keeps its label."""
        source = {key: value for key, value in source.items() if value not in (None, "")}
        key = _key(source)
        if key and key in self._by_key:
            return self._by_key[key], False
        series = "F" if source.get("region_id") else "E"
        label = self._next(series)
        self.sources[label] = source
        if key:
            self._by_key[key] = label
        return label, True

    def label_for(self, key: str) -> Optional[str]:
        return self._by_key.get(key)

    def cite(self, text: str, request=None) -> Tuple[List[dict], List[str]]:
        """`(citations, unknown labels)` of an answer, each in order of first appearance.

        With `request`, each citation is completed for the viewer (`citations.complete`): paragraph box, best
        sentence, quote, figure crop — where the reader may read them.
        """
        from apps.assistant import citations as resolver

        found: List[dict] = []
        unknown: List[str] = []
        for label, sentence in labels_with_sentences(text):
            source = self.sources.get(label)
            if source is None:
                unknown.append(label)
            elif request is not None:
                found.append(resolver.complete(label, source, sentence, request))
            else:
                found.append(citation(label, source))
        return found, unknown


def citation(label: str, source: dict) -> dict:
    """A source as a citation, without what `citations.complete` adds."""
    result = {"label": label}
    for key, value in source.items():
        if key in _PRIVATE:
            continue
        if key == "paragraph_id" and str(value).startswith("page:"):
            continue
        result[key] = value
    return result


def normalise(label: str) -> Optional[str]:
    match = _LABEL.fullmatch(label.strip())
    if not match:
        return None
    return f"{match.group(1).upper()}{int(match.group(2))}"


def labels_in(text: Optional[str]) -> List[str]:
    """Evidence labels cited in `text`, normalised (`e03` → `E3`), each once, in order of first appearance."""
    return [label for label, _sentence in labels_with_sentences(text)]


def labels_with_sentences(text: Optional[str]) -> List[Tuple[str, str]]:
    """Each label once, with the text of the sentence that cites it (everything since the previous sentence end)."""
    found: List[Tuple[str, str]] = []
    seen = set()
    text = text or ""
    for group in _CITATION.finditer(text):
        sentence = citing_sentence(text, group.start())
        for match in _LABEL.finditer(group.group(1)):
            label = f"{match.group(1).upper()}{int(match.group(2))}"
            if label not in seen:
                seen.add(label)
                found.append((label, sentence))
    return found


#: A sentence ends at end punctuation followed by space, or a line break.
_SENTENCE_END = re.compile(r"(?:[.!?…](?=\s)|\n)")


def citing_sentence(text: str, position: int) -> str:
    """The sentence a label at `position` belongs to: back to the previous sentence end.

    A label right after a full stop ("… pressure. [E2]") cites the sentence before the stop.
    """
    head = text[:position].rstrip()
    stripped = _CITATION.sub("", head).rstrip()
    if stripped.endswith((".", "!", "?", "…")):
        stripped = stripped[:-1]
    ends = list(_SENTENCE_END.finditer(stripped))
    start = ends[-1].end() if ends else 0
    return " ".join(stripped[start:].split())


def _labelled(ledger: Ledger, source: dict, text: Optional[str]) -> Tuple[str, dict]:
    label, _new = ledger.issue(source)
    if text:
        ledger.texts[label] = text
    return label, ledger.sources[label]


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
                "kind": PASSAGE,
                "entry_id": item.get("entry_id"),
                "title": item.get("title"),
                "page": passage.get("page"),
                "section": passage.get("section"),
                "paragraph_id": passage.get("paragraph_id"),
            }
            label, issued[label] = _labelled(ledger, source, passage["text"])
            passage["label"] = label
    _keep(payload, issued)
    return payload


def label_figures(payload: dict, ledger: Ledger, key: str = "figures") -> dict:
    """Label the figures (tables, equations) of a payload's `figures` list, in place: `F#` labels."""
    issued: Dict[str, dict] = {}
    for figure in payload.get(key) or []:
        if not figure.get("region_id"):
            continue
        source = {
            "kind": figure.get("kind") or "figure",
            "entry_id": figure.get("entry_id"),
            "title": figure.get("title"),
            "page": figure.get("page"),
            "region_id": figure.get("region_id"),
            "source_page": figure.get("source_page"),
        }
        label, issued[label] = _labelled(ledger, source, figure.get("caption"))
        figure["label"] = label
    _keep(payload, issued)
    return payload


def label_pages(payload: dict, ledger: Ledger) -> dict:
    """Label the pages of a `read_pdf_page` payload, in place (#127).

    A page is cited like a passage, as `[E#]` resolved to publication and page. A page read again keeps its
    label: its key stands in for a paragraph id.
    """
    issued: Dict[str, dict] = {}
    for page in payload.get("pages") or []:
        if not page.get("text"):
            continue
        source = {
            "kind": PAGE,
            "entry_id": payload.get("entry_id"),
            "title": payload.get("title"),
            "page": page.get("page"),
            "paragraph_id": f"page:{payload.get('entry_id')}:{page.get('page')}",
        }
        label, issued[label] = _labelled(ledger, source, page["text"])
        page["label"] = label
    _keep(payload, issued)
    return payload


def _keep(payload: dict, issued: Dict[str, dict]) -> None:
    if issued:
        payload.setdefault(EVIDENCE_KEY, {}).update(issued)


def _texts(payload: dict) -> Dict[str, str]:
    """Label → passage (or page, or caption) text of a labelled payload."""
    texts = {
        passage["label"]: passage["text"]
        for item in payload.get("items") or []
        for passage in item.get("passages") or []
        if passage.get("label") and passage.get("text")
    }
    for key in ("pages", "passages", "paragraphs"):
        for unit in payload.get(key) or []:
            if isinstance(unit, dict) and unit.get("label") and unit.get("text"):
                texts[unit["label"]] = unit["text"]
    for figure in payload.get("figures") or []:
        if isinstance(figure, dict) and figure.get("label") and figure.get("caption"):
            texts[figure["label"]] = figure["caption"]
    return texts


def strip(payload: dict, private=("paragraph_id", "region_id", "source_page")) -> dict:
    """A labelled payload as the model sees it: no evidence map, no ids it would only copy."""

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in private and key != EVIDENCE_KEY}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(payload)

"""Semantic highlight finding over the paragraph index (IP-018 Phase 6b).

The request is embedded with the model of the publication's extraction run
and every paragraph above `EVILFLOWERS_ASSISTANT_PARAGRAPH_MIN_SCORE` is a
candidate, expanded one hop over `next` and `similar` links. The model sees
all candidates at once as numbered sentences with their paragraph's score and
keeps the ones that answer the request. It never copies text, so every
highlight is a stored sentence with stored quads: nothing to resolve, nothing
paraphrased. The score decides what the model sees, never what is highlighted.

Returns None when the publication has no paragraph index yet, or the request
cannot be embedded; the caller then falls back to agentic search (Phase 6).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from django.conf import settings

from apps.assistant.explain import LANGUAGE_NAMES, _complete
from apps.assistant.highlight import MAX_QUOTES
from apps.assistant.llm import OllamaClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You mark sentences in a publication for a reader who will read them in a PDF viewer. The reader "
    "tells you what to look for. You are given candidate passages found by meaning, each sentence "
    "numbered, with a relevance score of its passage. Keep the sentences that answer the request or "
    "directly support the answer, and discard the rest. Keep a sentence only for what it says itself; "
    "a sentence that is merely on the topic is discarded. You may discard every sentence.\n\n"
    "Answer with a single JSON object and nothing else:\n"
    '{"summary": "<one sentence on what you marked>", '
    '"keep": [{"id": <sentence number>, "note": "<why it is relevant, one short sentence>"}]}'
)

#: Characters of candidate text per model call; longer candidate sets are sent in batches.
BATCH_CHARS = 24000

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Pick:
    text: str
    #: The reader's page.
    page: int
    quads: List[dict]
    size: Tuple[float, float]
    note: str
    score: float
    order: Tuple[int, int]


@dataclass
class SemanticResult:
    summary: str
    picks: List[Pick] = field(default_factory=list)
    candidates: int = 0
    sentences: int = 0
    tokens_used: int = 0


def find(document, *, prompt: str, language: Optional[str] = None) -> Optional[SemanticResult]:
    from apps.extraction.reader import EmbeddingUnavailable, ReaderExtraction

    extraction = ReaderExtraction.for_document(document)
    if extraction is None or not extraction.has_paragraphs():
        return None
    try:
        vector = extraction.embed(prompt)
    except EmbeddingUnavailable:
        logger.warning("Request embedding unavailable; falling back to agentic search", exc_info=True)
        return None

    candidates = extraction.search_paragraphs(
        vector,
        settings.EVILFLOWERS_ASSISTANT_PARAGRAPH_MIN_SCORE,
        settings.EVILFLOWERS_ASSISTANT_PARAGRAPH_CANDIDATES,
    )
    numbered = []
    for candidate in candidates:
        for sentence in candidate.sentences:
            text = candidate.paragraph.text[sentence.start : sentence.end].strip()
            if text and sentence.quads:
                numbered.append((candidate, sentence, text))

    result = SemanticResult(summary="", candidates=len(candidates), sentences=len(numbered))
    if not numbered:
        return result

    client = OllamaClient()
    kept = {}
    summaries = []
    for batch in _batches(numbered):
        content, _tool_calls, used = _complete(client, _messages(batch, prompt, language), None)
        result.tokens_used += used
        data = _json(content)
        if data.get("summary"):
            summaries.append(str(data["summary"]).strip())
        for keep in data.get("keep") or []:
            try:
                number = int(keep.get("id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if any(number == item[0] for item in batch) and number not in kept:
                kept[number] = str(keep.get("note") or "").strip()

    picks = []
    for number, note in kept.items():
        candidate, sentence, text = numbered[number - 1]
        picks.append(
            Pick(
                text=text,
                page=candidate.page,
                quads=sentence.quads,
                size=extraction.page_size(sentence.page),
                note=note,
                score=candidate.score,
                order=(candidate.paragraph.seq, sentence.seq),
            )
        )
    # MAX_QUOTES caps the sentences of one annotation: the best-scored are kept, in reading order.
    picks = sorted(sorted(picks, key=lambda pick: -pick.score)[:MAX_QUOTES], key=lambda pick: pick.order)
    result.picks = picks
    result.summary = summaries[0] if summaries else ""
    return result


def _batches(numbered):
    """`[(number, candidate, sentence, text)]` lists of at most BATCH_CHARS, best paragraphs first."""
    by_score = sorted(enumerate(numbered, start=1), key=lambda item: (-item[1][0].score, item[0]))
    batches, current, size = [], [], 0
    for number, (candidate, sentence, text) in by_score:
        if current and size + len(text) > BATCH_CHARS:
            batches.append(sorted(current))
            current, size = [], 0
        current.append((number, candidate, sentence, text))
        size += len(text)
    if current:
        batches.append(sorted(current))
    return batches


def _messages(batch, prompt: str, language: Optional[str]) -> List[dict]:
    lines = []
    current = None
    for number, candidate, _sentence, text in batch:
        if candidate is not current:
            section = f" — {candidate.paragraph.section}" if candidate.paragraph.section else ""
            lines.append(f"\n[page {candidate.page}{section}, score {candidate.score:.2f}]")
            current = candidate
        lines.append(f"{number}. {text}")

    request = f'The reader asks you to mark:\n"""\n{prompt}\n"""\n\nCandidate sentences:' + "\n".join(lines)
    name = LANGUAGE_NAMES.get((language or "").split("-")[0].lower())
    if name:
        request += f"\n\nWrite `summary` and every `note` in {name}."
        if name == "Slovak":
            request += " It must be Slovak, never Czech."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]


def _json(content: str) -> dict:
    match = _JSON_OBJECT.search(content or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

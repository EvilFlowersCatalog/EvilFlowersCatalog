"""Study mode: outline, section summary, walkthrough, quiz and key concepts (IP-030 §6).

One tool, `study(mode, entry_id?, section?, part?)`. The section's text comes from the active extraction run
(`library.Text`), at most `EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES` pages per turn (Q8); a longer section is
split into parts, and the walkthrough's "next" step continues it. Its paragraphs are labelled into the chat's
ledger like any passage, so every point, question and concept carries citations.

- `outline` lists the sections (no model call).
- `summary` returns the section's generated summary (`sections.summary_for`, cached), with a few labelled
  passages to cite; the model then answers in its own words.
- `walkthrough`, `quiz`, `key_concepts` generate a structured artefact on a no-tools request constrained to its
  JSON Schema (`structured.parse_object`, one retry). The artefact goes to the reader as a `study` frame and is
  the turn's answer: the portal renders it as an interactive card, the model does not restate it.

An item whose labels are missing or unknown is dropped; an artefact with fewer than two valid items fails, and
the model is told so. Readium LCP publications are refused: their text never reaches the model.
"""

import json
import logging
import re
from typing import Callable, Dict, List, Optional

from django.conf import settings

from apps.assistant import prompts, structured
from apps.assistant.library import Text, ToolContext, entry_for, label_units
from apps.mcp.errors import ToolError

logger = logging.getLogger(__name__)

MODES = ("outline", "summary", "walkthrough", "quiz", "key_concepts")
#: Modes whose artefact is the answer (the turn closes on it).
ARTEFACTS = ("walkthrough", "quiz", "key_concepts")

#: Paragraphs and characters of a section given to the model per part.
MAX_PARAGRAPHS = 40
MAX_CHARS = 9000
#: Passages shown with a summary, for citing it.
SUMMARY_PASSAGES = 4
#: Valid items an artefact needs at least.
MIN_ITEMS = 2

SPECIFICATION = (
    "A publication's sections (outline), or one section as a summary, walkthrough, quiz or key_concepts; the "
    "last three are shown to the reader as a card.",
    {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(MODES)},
            "entry_id": {"type": "string", "description": "Omit for the chat's publication."},
            "section": {"type": "string", "description": "As outline lists it."},
            "topic": {"type": "string", "description": "Subject asked about, if no section"},
            "part": {"type": "integer"},
        },
        "required": ["mode"],
    },
)

_LABELS = {"type": "array", "items": {"type": "string"}}

SCHEMAS = {
    "walkthrough": {
        "type": "object",
        "properties": {
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "labels": _LABELS},
                    "required": ["text", "labels"],
                },
            }
        },
        "required": ["points"],
    },
    "quiz": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "answer_index": {"type": "integer"},
                        "explanation": {"type": "string"},
                        "labels": _LABELS,
                        "figure": {"type": "string"},
                    },
                    "required": ["question", "options", "answer_index", "explanation", "labels"],
                },
            }
        },
        "required": ["questions"],
    },
    "key_concepts": {
        "type": "object",
        "properties": {
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"term": {"type": "string"}, "definition": {"type": "string"}, "labels": _LABELS},
                    "required": ["term", "definition", "labels"],
                },
            }
        },
        "required": ["concepts"],
    },
}

#: The list each artefact's items are in.
ITEMS = {"walkthrough": "points", "quiz": "questions", "key_concepts": "concepts"}

PROMPTS = {mode: prompts.render(f"study_{mode}") for mode in ARTEFACTS}

RETRY = "Your answer was not valid: every item needs `labels` from the paragraphs given (E1, E2 …). Answer again."


#: Modes that cover a chapter with its subsections, sampled across them (#166); the others read one section.
#: Not `key_concepts`: its terms are recorded as defined by the section read (`_record_definitions`), and a
#: chapter's span would file every subsection's terms under the chapter heading.
SPANNING = ("quiz",)
#: Figures a quiz is offered to ask about.
QUIZ_FIGURES = 4


def study(context: ToolContext, arguments: dict, client=None) -> dict:
    from apps.extraction.outline import end_seq, find, first_content, following, outline, span

    mode = str(arguments.get("mode") or "").strip().lower()
    if mode not in MODES:
        raise ToolError(f"`mode` must be one of: {', '.join(MODES)}.")
    entry = entry_for(context, arguments.get("entry_id"))
    text = Text.of(context, entry)
    sections = outline(text.run)
    if not sections:
        raise ToolError("This publication has no sections in its processed text; use read_pdf_page instead.")

    if mode == "outline":
        return _outline(text, sections)

    wanted = str(arguments.get("section") or "").strip() or None
    topic = str(arguments.get("topic") or "").strip() or None
    if wanted:
        section = find(sections, wanted)
        if section is None:
            names = "; ".join(item.title[:80] for item in sections[:20])
            raise ToolError(f"No section named {wanted!r}. Sections: {names}")
    else:
        # Not the copyright page (#166): the section the topic is about, else the first one with content.
        section = (_by_topic(context, text, sections, topic) if topic else None) or first_content(sections)
    try:
        part = max(1, int(arguments.get("part") or 1))
    except (TypeError, ValueError):
        part = 1

    covered = span(sections, section) if mode in SPANNING else [section]
    spread = len(covered) > 1
    passages, has_more = _section_passages(
        context, text, section.first_seq, end_seq(sections, covered[-1]), part, spread
    )
    if not passages:
        raise ToolError("Nothing readable in this part of the section.")
    figures = _figures(text, passages) if mode == "quiz" and settings.EVILFLOWERS_ASSISTANT_QUIZ_FIGURES else []
    issued = label_units(context.ledger, passages + figures)
    base = {
        "mode": mode,
        "entry_id": str(entry.pk),
        "title": entry.title,
        "section": section.title,
        "page_range": [passages[0]["page"], passages[-1]["page"]],
        "part": part,
        "has_more": has_more,
    }
    if spread:
        base["sections"] = [item.title for item in covered]

    from apps.assistant.llm import LLMUnavailable

    try:
        if mode == "summary":
            return _summary(context, text, section, passages, issued, base, client)
        data = _generate(
            mode, section.title, passages, context.language, set(issued), client, context.book, figures=figures
        )
    except LLMUnavailable as error:
        # Its own request found the model busy (every llama-server slot leased) or down: the turn goes on, and
        # the model answers from the labelled passages instead.
        logger.warning("Study %s: model unavailable (%s)", mode, error)
        raise ToolError(
            "The model is busy, so this could not be prepared now. Answer from the passages of the section "
            "(search it), and offer to try again."
        ) from error
    if data is None:
        raise ToolError(
            f"The {mode.replace('_', ' ')} could not be prepared from this section. Answer from the passages "
            "instead, citing their labels."
        )
    artefact = {**base, **data}
    if mode == "walkthrough":
        upcoming = following(sections, section)
        artefact["next"] = (
            {"section": section.title, "part": part + 1}
            if has_more
            else ({"section": upcoming.title, "part": 1} if upcoming else None)
        )
        # "Read first" holds earlier sections and other books only; a section further on in the same book that
        # defines the term goes to `defined_later`, never presented as something to read before (#153).
        hints = _prerequisites(context, text, section)
        artefact["prerequisites"] = [{k: v for k, v in h.items() if k != "later"} for h in hints if not h["later"]]
        artefact["defined_later"] = [{k: v for k, v in h.items() if k != "later"} for h in hints if h["later"]]
    if mode == "key_concepts":
        _record_definitions(text, section, data)

    frame = {"mode": mode, "data": artefact}
    context.frames.append(("study", frame))
    payload = {**base, "study": frame, "passages": passages, "evidence": issued}
    if figures:
        payload["figures"] = figures
    return payload


def _by_topic(context: ToolContext, text: Text, sections, topic: str):
    """The section of this publication whose passages answer `topic` best (#166); None when nothing matches."""
    from collections import Counter

    from apps.extraction.discovery import search_passages
    from apps.extraction.outline import is_matter

    try:
        result = search_passages(
            context.request, topic, limit=1, per_entry=8, scope={"id": str(text.entry.pk)}, surface="assistant"
        )
    except Exception:
        logger.exception("Study topic search failed for %r", topic[:60])
        return None
    scores: Counter = Counter()
    for hit in result.items:
        for passage in hit.passages:
            if passage.section:
                scores[passage.section] += passage.score if passage.score else 1.0
    by_title = {section.title: section for section in sections if not is_matter(section)}
    for title, _score in scores.most_common():
        if title in by_title:
            return by_title[title]
    return None


def _figures(text: Text, passages: List[dict]) -> List[dict]:
    """Figures and tables on the pages a quiz reads, for questions about them (#166)."""
    from apps.extraction.models import Region

    source_pages = {passage.get("source_page") or passage["page"] for passage in passages}
    regions = (
        Region.objects.filter(run=text.run, page__in=source_pages, kind__in=("figure", "table"))
        .exclude(image_path__isnull=True)
        .order_by("page", "seq")[:QUIZ_FIGURES]
    )
    return [figure for figure in (text.figure(region) for region in regions) if figure is not None]


def _outline(text: Text, sections) -> dict:
    from apps.extraction.outline import MAX_SECTIONS

    lines = []
    for section in sections[:MAX_SECTIONS]:
        first, last = text.page_of(section.first_page), text.page_of(section.last_page)
        if first is None:
            continue
        lines.append({"section": section.title[:120], "pages": f"{first}-{last or first}"})
    payload = {"mode": "outline", "entry_id": str(text.entry.pk), "title": text.entry.title, "sections": lines}
    if len(sections) > MAX_SECTIONS:
        payload["note"] = f"{len(sections)} sections; the first {MAX_SECTIONS} are listed."
    return payload


def _section_passages(context: ToolContext, text: Text, start: int, end: Optional[int], part: int, spread=False):
    """The labelled-to-be passages of one part of a section, and whether more parts follow.

    The section is the paragraphs from `start` up to `end` (`seq`, end excluded; None: to the end): one heading, or
    a chapter with its subsections. Read in parts of `EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES` pages; with `spread`
    (a chapter for a quiz, #166) the pages are sampled across all of it instead, so the questions cover every
    subsection and not only the chapter's first pages.
    """
    per_part = max(int(settings.EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES), 1)
    paragraphs = text.run.paragraphs.filter(seq__gte=start)
    if end is not None:
        paragraphs = paragraphs.filter(seq__lt=end)
    paragraphs = list(paragraphs.order_by("seq"))
    pages = list(dict.fromkeys(paragraph.page for paragraph in paragraphs))
    if spread and len(pages) > per_part:
        step = len(pages) / per_part
        wanted = {pages[int(index * step)] for index in range(per_part)}
        has_more = False
    else:
        wanted = set(pages[(part - 1) * per_part : part * per_part])
        has_more = len(pages) > part * per_part
    if not wanted:
        raise ToolError(f"The section has {max(1, -(-len(pages) // per_part))} part(s).")

    read = context.study_pages.setdefault(str(text.entry.pk), [])
    new = [page for page in sorted(wanted) if page not in read]
    if len(set(read) | set(new)) > per_part:
        raise ToolError("Study reading budget for this answer reached; continue in the next message.")
    read.extend(new)

    # Spread across pages, each page gets its share of the size limits, so the last ones are not cut off.
    page_limit = -(-MAX_PARAGRAPHS // len(wanted)) if spread else MAX_PARAGRAPHS
    page_chars = MAX_CHARS // len(wanted) if spread else MAX_CHARS
    passages, size, per_page = [], 0, {}
    for paragraph in paragraphs:
        if paragraph.page not in wanted:
            continue
        taken, chars = per_page.get(paragraph.page, (0, 0))
        if spread and (taken >= page_limit or chars >= page_chars):
            continue
        passage = text.passage(paragraph, chars=900)
        if passage is None:
            continue
        passages.append(passage)
        per_page[paragraph.page] = (taken + 1, chars + len(passage["text"]))
        size += len(passage["text"])
        if len(passages) >= MAX_PARAGRAPHS or size >= MAX_CHARS:
            break
    return passages, has_more


def _summary(context, text: Text, section, passages, issued, base, client) -> dict:
    from apps.assistant.sections import summary_for

    row = summary_for(text.run, text.entry, section, client=client, book=context.book)
    shown = [{**passage, "text": passage["text"][:300]} for passage in passages[:SUMMARY_PASSAGES]]
    payload = {
        **base,
        "passages": shown,
        "evidence": {passage["label"]: issued[passage["label"]] for passage in shown},
    }
    if row is None:
        payload["note"] = "No summary could be made; answer from the passages."
        return payload
    payload["summary"] = row.summary
    payload["concepts"] = [item.get("term") for item in row.concepts or [] if isinstance(item, dict)]
    payload["note"] = "The summary is generated and unchecked: explain it in your words and cite the passages."
    return payload


def _content(section: str, passages: List[dict], language: Optional[str], figures: Optional[List[dict]] = None) -> str:
    lines = [f'Section: "{section}"', ""]
    for passage in passages:
        lines.append(f"[{passage['label']}] (page {passage['page']}) {passage['text']}")
    if figures:
        lines.append("")
        lines.append("Figures and tables the reader can be shown:")
        for figure in figures:
            caption = figure.get("caption") or "no caption"
            lines.append(f"[{figure['label']}] ({figure.get('kind') or 'figure'}, page {figure['page']}) {caption}")
    name = prompts.language_name(language)
    lines.append("")
    lines.append(
        f"Write in {name}.{prompts.not_czech(language)}" if name else "Write in the language of the paragraphs."
    )
    return "\n".join(lines)


def _generate(
    mode: str, section: str, passages, language, labels: set, client=None, book=None, figures=None
) -> Optional[dict]:
    from apps.assistant.evidence import normalise

    if client is None:
        # Its own request (and slot): a study call never displaces the chat's cached state.
        from apps.assistant.llm import client as make_client

        client = make_client()
    messages = [
        {"role": "system", "content": PROMPTS[mode]},
        {"role": "user", "content": _content(section, passages, language, figures)},
    ]
    for attempt in range(2):
        content = ""
        for delta in client.chat(
            messages, response_format=SCHEMAS[mode], max_tokens=settings.EVILFLOWERS_ASSISTANT_STUDY_NUM_PREDICT
        ):
            content += delta.content
            if book is not None:
                book(delta)
        data = structured.parse_object(content, lambda value: isinstance(value.get(ITEMS[mode]), list))
        items = _valid_items(mode, data, labels, normalise) if data else []
        if len(items) >= MIN_ITEMS:
            return {ITEMS[mode]: items}
        messages = messages + [{"role": "assistant", "content": content}, {"role": "user", "content": RETRY}]
    logger.warning("Study %s failed for section %r", mode, section[:60])
    return None


def _valid_items(mode: str, data: dict, labels: set, normalise: Callable) -> List[dict]:
    valid = []
    for item in data.get(ITEMS[mode]) or []:
        if not isinstance(item, dict):
            continue
        cited = [normalise(str(label)) for label in item.get("labels") or []]
        cited = list(dict.fromkeys(label for label in cited if label))
        if not cited or not set(cited) <= labels:
            continue
        if mode == "walkthrough" and str(item.get("text") or "").strip():
            # A walkthrough point cites inline, as the chat does (`_citations.md`): its labels stay.
            valid.append({"text": item["text"].strip(), "labels": cited})
        elif mode == "quiz":
            options = [unlabel(str(option), labels) for option in item.get("options") or []]
            try:
                answer = int(item.get("answer_index"))
            except (TypeError, ValueError):
                continue
            if len(options) == 4 and all(options) and 0 <= answer < 4 and str(item.get("question") or "").strip():
                question = {
                    "question": unlabel(item["question"], labels),
                    "options": options,
                    "answer_index": answer,
                    "explanation": unlabel(str(item.get("explanation") or ""), labels),
                    "labels": cited,
                }
                # A question about a figure shows it (#166): its F# label, also among the labels so the card's
                # citation carries the crop.
                figure = normalise(str(item.get("figure") or ""))
                if figure and figure.startswith("F") and figure in labels:
                    question["figure"] = figure
                    if figure not in cited:
                        question["labels"] = [*cited, figure]
                valid.append(question)
        elif mode == "key_concepts" and str(item.get("term") or "").strip():
            valid.append(
                {
                    "term": unlabel(item["term"], labels),
                    "definition": unlabel(str(item.get("definition") or ""), labels),
                    "labels": cited,
                }
            )
    return valid


#: Evidence labels written into a card's text: "[E1]", "(E1, E2)", "v odseku E1", "in passage E3" (#166).
_LABEL_MENTION = re.compile(
    r"\s*(?:\[\s*[EF]\d+(?:\s*[,;]\s*[EF]\d+)*\s*\]|\(\s*[EF]\d+(?:\s*[,;]\s*[EF]\d+)*\s*\)|"
    r"\b(?:v|vo|na|in|see|pozri|podľa|according to)?\s*"
    r"(?:odseku|odsek|odseky|odsekoch|pasáži|pasáže|úryvku|paragraph|paragraphs|passage|passages|label)\s+"
    r"[EF]\d+(?:\s*(?:,|a|and)\s*[EF]\d+)*\b)",
    re.IGNORECASE,
)


def unlabel(text: str, labels: Optional[set] = None) -> str:
    """A card's text without the evidence labels the model wrote into it: the portal adds the citation chip.

    With `labels`, only mentions of those (the labels given for the card): "(F5)" in a software book is a key.
    """
    from apps.assistant.evidence import _LABEL

    def drop(match: re.Match) -> str:
        if labels is None:
            return ""
        named = {f"{m.group(1).upper()}{int(m.group(2))}" for m in _LABEL.finditer(match.group(0))}
        return "" if named and named <= labels else match.group(0)

    cleaned = _LABEL_MENTION.sub(drop, str(text or ""))
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    return " ".join(cleaned.split())


def _prerequisites(context: ToolContext, text: Text, section) -> List[dict]:
    """Sections that define what this one only discusses (the graph), as "read before" hints."""
    try:
        from apps.graph import projection, queries

        if not projection.available():
            return []
        found = queries.prerequisites(
            projection.section_key(text.entry.pk, section.title), queries.visible_entry_ids(context.request)
        )
    except Exception:
        logger.exception("Prerequisites of section %r failed", section.title[:60])
        return []
    return [
        {"entry_id": item["entry_id"], "section": item["title"], "page": item["first_page"], "later": item["later"]}
        for item in found
    ]


def _record_definitions(text: Text, section, data: dict) -> None:
    try:
        from apps.graph import projection

        projection.add_definitions(
            text.run.pk, section.title, [item["term"] for item in data.get("concepts") or []], text.entry
        )
    except Exception:
        logger.exception("Could not record the concepts of section %r", section.title[:60])


def model_view(payload: dict) -> dict:
    """What the model sees of a study result: the artefact is shown to the reader, not repeated to the model."""
    from apps.assistant.evidence import strip

    if payload.get("mode") in ARTEFACTS:
        artefact = (payload.get("study") or {}).get("data") or {}
        items = artefact.get(ITEMS[payload["mode"]]) or []
        view = {
            "shown": payload["mode"],
            "section": payload.get("section"),
            "pages": payload.get("page_range"),
            "items": [_short(payload["mode"], item) for item in items],
            "next": artefact.get("next"),
            "note": "The reader sees this as an interactive card.",
        }
        return view
    return strip(payload)


def _short(mode: str, item: dict) -> str:
    if mode == "walkthrough":
        return item.get("text", "")[:160]
    if mode == "quiz":
        return item.get("question", "")[:160]
    return item.get("term", "")


def as_text(payload: dict) -> str:
    return json.dumps(model_view(payload), ensure_ascii=False, default=str)

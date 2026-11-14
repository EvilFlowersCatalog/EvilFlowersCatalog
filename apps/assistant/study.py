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
            "section": {"type": "string", "description": "As outline lists it; omit for the first."},
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


def study(context: ToolContext, arguments: dict, client=None) -> dict:
    from apps.extraction.outline import find, following, outline

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
    section = find(sections, wanted)
    if section is None:
        names = "; ".join(item.title[:80] for item in sections[:20])
        raise ToolError(f"No section named {wanted!r}. Sections: {names}")
    try:
        part = max(1, int(arguments.get("part") or 1))
    except (TypeError, ValueError):
        part = 1

    passages, has_more = _section_passages(context, text, section, part)
    if not passages:
        raise ToolError("Nothing readable in this part of the section.")
    issued = label_units(context.ledger, passages)
    base = {
        "mode": mode,
        "entry_id": str(entry.pk),
        "title": entry.title,
        "section": section.title,
        "page_range": [passages[0]["page"], passages[-1]["page"]],
        "part": part,
        "has_more": has_more,
    }

    from apps.assistant.llm import LLMUnavailable

    try:
        if mode == "summary":
            return _summary(context, text, section, passages, issued, base, client)
        data = _generate(mode, section.title, passages, context.language, set(issued), client, context.book)
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
        artefact["prerequisites"] = _prerequisites(context, text, section)
    if mode == "key_concepts":
        _record_definitions(text, section, data)

    frame = {"mode": mode, "data": artefact}
    context.frames.append(("study", frame))
    payload = {**base, "study": frame, "passages": passages, "evidence": issued}
    return payload


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


def _section_passages(context: ToolContext, text: Text, section, part: int):
    """The labelled-to-be passages of one part of a section, and whether more parts follow."""
    per_part = max(int(settings.EVILFLOWERS_ASSISTANT_STUDY_MAX_PAGES), 1)
    paragraphs = list(text.run.paragraphs.filter(section=section.title).order_by("seq"))
    pages = list(dict.fromkeys(paragraph.page for paragraph in paragraphs))
    wanted = set(pages[(part - 1) * per_part : part * per_part])
    if not wanted:
        raise ToolError(f"The section has {max(1, -(-len(pages) // per_part))} part(s).")

    read = context.study_pages.setdefault(str(text.entry.pk), [])
    new = [page for page in sorted(wanted) if page not in read]
    if len(set(read) | set(new)) > per_part:
        raise ToolError("Study reading budget for this answer reached; continue in the next message.")
    read.extend(new)

    passages, size = [], 0
    for paragraph in paragraphs:
        if paragraph.page not in wanted:
            continue
        passage = text.passage(paragraph, chars=900)
        if passage is None:
            continue
        passages.append(passage)
        size += len(passage["text"])
        if len(passages) >= MAX_PARAGRAPHS or size >= MAX_CHARS:
            break
    return passages, len(pages) > part * per_part


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


def _content(section: str, passages: List[dict], language: Optional[str]) -> str:
    lines = [f'Section: "{section}"', ""]
    for passage in passages:
        lines.append(f"[{passage['label']}] (page {passage['page']}) {passage['text']}")
    name = prompts.language_name(language)
    lines.append("")
    lines.append(
        f"Write in {name}.{prompts.not_czech(language)}" if name else "Write in the language of the paragraphs."
    )
    return "\n".join(lines)


def _generate(mode: str, section: str, passages, language, labels: set, client=None, book=None) -> Optional[dict]:
    from apps.assistant.evidence import normalise

    if client is None:
        # Its own request (and slot): a study call never displaces the chat's cached state.
        from apps.assistant.llm import client as make_client

        client = make_client()
    messages = [
        {"role": "system", "content": PROMPTS[mode]},
        {"role": "user", "content": _content(section, passages, language)},
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
            valid.append({"text": item["text"].strip(), "labels": cited})
        elif mode == "quiz":
            options = [str(option).strip() for option in item.get("options") or []]
            try:
                answer = int(item.get("answer_index"))
            except (TypeError, ValueError):
                continue
            if len(options) == 4 and all(options) and 0 <= answer < 4 and str(item.get("question") or "").strip():
                valid.append(
                    {
                        "question": item["question"].strip(),
                        "options": options,
                        "answer_index": answer,
                        "explanation": str(item.get("explanation") or "").strip(),
                        "labels": cited,
                    }
                )
        elif mode == "key_concepts" and str(item.get("term") or "").strip():
            valid.append(
                {
                    "term": item["term"].strip(),
                    "definition": str(item.get("definition") or "").strip(),
                    "labels": cited,
                }
            )
    return valid


def _prerequisites(context: ToolContext, text: Text, section) -> List[dict]:
    """Sections that define what this one only discusses (the graph), as "read before" hints."""
    try:
        from apps.graph import projection, queries

        if not projection.available():
            return []
        found = queries.prerequisites(
            projection.section_key(text.run.pk, section.title), queries.visible_entry_ids(context.request)
        )
    except Exception:
        logger.exception("Prerequisites of section %r failed", section.title[:60])
        return []
    return [{"entry_id": item["entry_id"], "section": item["title"], "page": item["first_page"]} for item in found]


def _record_definitions(text: Text, section, data: dict) -> None:
    try:
        from apps.graph import projection

        projection.add_definitions(text.run.pk, section.title, [item["term"] for item in data.get("concepts") or []])
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

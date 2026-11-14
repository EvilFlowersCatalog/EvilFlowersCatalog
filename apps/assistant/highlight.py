"""Highlight the passages of a publication that answer a reader's request (IP-018).

The reader asks for something ("where does the author define entropy?",
"what supports the claim that X?") and the agent finds it the way the explain
agent finds context: it searches the reader's copy with `search_pdf`, reads
the pages that look promising with `read_pdf_page`, and answers with exact
quotes, each with the page it read it on.

The agent is hidden: nothing of the conversation is stored, no chat is created.
What remains is the agent annotation the task writes from the quotes.

Only quotes that occur in a page the agent actually read are kept
(`quotes.quote_in_text`); a paraphrase cannot be checked against the book and
cannot be placed on the page.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from apps.assistant.explain import (
    LANGUAGE_NAMES,
    MAX_TOOL_ROUNDS,
    READ_PDF_PAGE,
    SEARCH_PARAGRAPHS,
    SEARCH_PDF,
    TOOL_SPECIFICATIONS,
    _complete,
    run_document_tool,
)
from apps.assistant import prompts, structured
from apps.assistant.llm import client as make_client
from apps.assistant.pdf import ReaderDocument
from apps.assistant.quotes import collapse, quote_in_text

logger = logging.getLogger(__name__)

#: Quotes kept per request; the rest of a longer answer is dropped.
MAX_QUOTES = 20

#: `prompts/highlight.md` (#116).
SYSTEM_PROMPT = prompts.render(
    "highlight",
    search_paragraphs=SEARCH_PARAGRAPHS,
    search_pdf=SEARCH_PDF,
    read_pdf_page=READ_PDF_PAGE,
    max_quotes=MAX_QUOTES,
)


#: The answer's shape, for Ollama's `format` on the retry without tools (IP-022).
QUOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"page": {"type": "integer"}, "text": {"type": "string"}, "note": {"type": "string"}},
                "required": ["page", "text"],
            },
        },
    },
    "required": ["summary", "quotes"],
}


@dataclass
class Quote:
    page: int
    text: str
    note: str = ""


@dataclass
class Highlights:
    summary: str
    quotes: List[Quote] = field(default_factory=list)
    #: Quotes dropped because they were not on any page the agent read.
    rejected: int = 0
    pages: List[int] = field(default_factory=list)
    tokens_used: int = 0


def highlight(
    document: ReaderDocument,
    *,
    prompt: str,
    title: str,
    authors: str,
    language: Optional[str] = None,
) -> Highlights:
    client = make_client()
    pages_read: List[int] = []

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _opening(document, prompt, title, authors, language)},
    ]
    tokens = 0
    content = ""

    for _round in range(MAX_TOOL_ROUNDS):
        content, tool_calls, used = _complete(client, messages, TOOL_SPECIFICATIONS)
        tokens += used
        if not tool_calls:
            break
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            messages.append({"role": "tool", "content": run_document_tool(document, call, pages_read)})
    else:
        content = ""

    data = _json(content)
    if data is None:
        # Out of rounds, or prose instead of JSON: ask once more for the answer only.
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": "Stop reading now and answer with the JSON object only."})
        content, _tool_calls, used = _complete(client, messages, None, QUOTES_SCHEMA)
        tokens += used
        data = _json(content) or {}

    return _gate(document, data, pages_read, tokens)


def _opening(document: ReaderDocument, prompt: str, title: str, authors: str, language: Optional[str]) -> str:
    parts = [f'Publication: "{title}"' + (f" by {authors}" if authors else "")]
    parts.append(f"It has {document.page_count} pages.")

    outline = document.outline()
    if outline:
        lines = [f"{'  ' * (heading['level'] - 1)}{heading['title']} (page {heading['page']})" for heading in outline]
        parts.append("Outline:\n" + "\n".join(lines[:200]))

    parts.append(f'The reader asks you to mark:\n"""\n{prompt}\n"""')

    name = prompts.language_name(language)
    if name:
        parts.append(
            f"Write `summary` and every `note` in {name}; keep quotes in the language of the publication."
            + prompts.not_czech(language)
        )
    return "\n\n".join(parts)


def _json(content: str) -> Optional[dict]:
    return structured.parse_object(content, lambda data: isinstance(data.get("quotes"), list))


def _gate(document: ReaderDocument, data: dict, pages_read: List[int], tokens: int) -> Highlights:
    """Keep the quotes found on a page the agent read; correct the page when it named the wrong one."""
    kept: List[Quote] = []
    seen = set()
    rejected = 0

    for raw in data.get("quotes") or []:
        if not isinstance(raw, dict):
            continue
        text = collapse(str(raw.get("text") or ""))
        if not text:
            continue
        try:
            page = int(raw.get("page"))
        except (TypeError, ValueError):
            page = None

        candidates = ([page] if page in pages_read else []) + [number for number in pages_read if number != page]
        found = next((number for number in candidates if quote_in_text(text, document.page(number) or "")), None)
        if found is None:
            rejected += 1
            continue

        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(Quote(page=found, text=text, note=str(raw.get("note") or "").strip()))

    if rejected:
        logger.info("Highlight agent: %d of %d quotes were not on a page it read", rejected, rejected + len(kept))

    return Highlights(
        summary=str(data.get("summary") or "").strip(),
        quotes=kept[:MAX_QUOTES],
        rejected=rejected,
        pages=pages_read,
        tokens_used=tokens,
    )

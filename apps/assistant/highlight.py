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

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from apps.assistant.explain import (
    LANGUAGE_NAMES,
    MAX_TOOL_ROUNDS,
    READ_PDF_PAGE,
    SEARCH_PDF,
    TOOL_SPECIFICATIONS,
    _complete,
    run_document_tool,
)
from apps.assistant.llm import OllamaClient
from apps.assistant.pdf import ReaderDocument
from apps.assistant.quotes import collapse, quote_in_text

logger = logging.getLogger(__name__)

#: Quotes kept per request; the rest of a longer answer is dropped.
MAX_QUOTES = 20

SYSTEM_PROMPT = (
    "You find and mark passages in a publication for a reader who will read them in a PDF viewer. "
    "The reader tells you what to look for. You do not explain or summarise the book: you point the "
    "reader to the exact sentences so they read them themselves.\n\n"
    f"Search the text with `{SEARCH_PDF}`. Try several queries: the reader's own words, synonyms, "
    "related terms, and the terms the book itself uses once you have seen them. Search results are "
    f"only snippets; read every page you quote from with `{READ_PDF_PAGE}`. The outline helps you "
    "find the right chapter.\n\n"
    "Quote only what the pages say, copied character for character from the page text you read: "
    "one to three consecutive sentences per quote, never shortened, merged or reworded. A quote "
    "that is not literally on its page is discarded.\n\n"
    "When you are done reading, answer with a single JSON object and nothing else:\n"
    '{"summary": "<one sentence on what you marked>", '
    '"quotes": [{"page": <the page you read it on>, "text": "<the exact sentence(s)>", '
    '"note": "<why it is relevant, one short sentence>"}]}\n'
    f"Give the quotes in reading order, at most {MAX_QUOTES}. Return an empty list when the "
    "publication does not address the request."
)


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
    client = OllamaClient()
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
        content, _tool_calls, used = _complete(client, messages, None)
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

    name = LANGUAGE_NAMES.get((language or "").split("-")[0].lower())
    if name:
        instruction = f"Write `summary` and every `note` in {name}; keep quotes in the language of the publication."
        if name == "Slovak":
            instruction += " It must be Slovak, never Czech."
        parts.append(instruction)
    return "\n\n".join(parts)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _json(content: str) -> Optional[dict]:
    match = _JSON_OBJECT.search(content or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("quotes"), list) else None


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

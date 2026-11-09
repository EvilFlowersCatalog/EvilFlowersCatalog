"""Explain a passage the reader selected, by reading the book it came from.

A selected sentence rarely stands on its own — "this is the best method so far"
says nothing about which method. The model is given the page the selection is
on and two tools over the reader's own copy of the publication, and reads
whatever else it needs before answering:

- `read_pdf_page` returns the text of a page, numbered as the viewer numbers it;
- `search_pdf` finds the pages that mention a term, with a snippet of each.

Unlike a chat turn nothing is persisted: the request carries everything, the
loop runs in memory and the answer is returned whole.
"""

import json
import uuid
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from django.conf import settings

from apps.assistant import prompts, structured
from apps.assistant.llm import OllamaClient
from apps.assistant.pdf import ReaderDocument

logger = logging.getLogger(__name__)

#: Runaway guard only. Tool calls are not what is limited (IP-018 Q5); reading
#: is, by `EVILFLOWERS_ASSISTANT_MAX_PAGES_READ`, and this sits well above what
#: that budget allows so it never cuts a legitimate explanation short.
MAX_TOOL_ROUNDS = 25

#: Pages a single `read_pdf_page` call may return.
MAX_PAGES_PER_READ = 3

#: Page text is clipped to this many characters before it reaches the model,
#: so one dense page cannot push the rest of the context out of the window.
MAX_PAGE_CHARS = 6000

READ_PDF_PAGE = "read_pdf_page"
SEARCH_PDF = "search_pdf"
LIST_PAGE_REGIONS = "list_page_regions"
GET_REGION = "get_region"
LIST_REGIONS = "list_regions"
SEARCH_PARAGRAPHS = "search_paragraphs"

#: Paragraphs `search_paragraphs` returns, best first.
MAX_PARAGRAPH_RESULTS = 8

#: Characters of each paragraph `search_paragraphs` returns.
MAX_PARAGRAPH_CHARS = 1200

#: `prompts/explain.md` (#116).
SYSTEM_PROMPT = prompts.render(
    "explain", search_paragraphs=SEARCH_PARAGRAPHS, search_pdf=SEARCH_PDF, read_pdf_page=READ_PDF_PAGE
)

#: Tools run against the reader's copy, in explain, highlight and bound chats.
DOCUMENT_TOOLS = (READ_PDF_PAGE, SEARCH_PDF, LIST_PAGE_REGIONS, GET_REGION, LIST_REGIONS, SEARCH_PARAGRAPHS)

TOOL_SPECIFICATIONS = [
    {
        "type": "function",
        "function": {
            "name": READ_PDF_PAGE,
            "description": (
                "Read the text of pages of the publication, numbered as the reader's viewer numbers "
                f"them. Returns at most {MAX_PAGES_PER_READ} pages per call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "First page to read, 1-based."},
                    "count": {
                        "type": "integer",
                        "description": f"How many consecutive pages to read, 1-{MAX_PAGES_PER_READ}. Defaults to 1.",
                    },
                },
                "required": ["page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SEARCH_PDF,
            "description": (
                "Search the publication's full text for words. Returns the best matching pages with a "
                "snippet around the match. Use it to find where a term, name or method is introduced. "
                "With `regex: true` the query is a regular expression (Python syntax, case-insensitive) "
                "and every page with a match is returned in reading order with its match count, e.g. "
                r"`table\s+2\.\d` or `scaffold(ing|ed)`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words or a phrase, or a regular expression."},
                    "regex": {
                        "type": "boolean",
                        "description": "Treat `query` as a regular expression. Defaults to false.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_PAGE_REGIONS,
            "description": (
                "List the figures, tables and equations the catalog found on a page (IP-018), each "
                "with its id, kind and caption or first line. Empty when the publication has not been "
                "processed or the page has none."
            ),
            "parameters": {
                "type": "object",
                "properties": {"page": {"type": "integer", "description": "Page, as the reader's viewer numbers it."}},
                "required": ["page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_REGIONS,
            "description": (
                "List every figure, table and equation the catalog found in the publication, with id, kind, "
                "page and caption, in reading order. Use it to see all figures at once or to find one by its "
                "caption; cite one as [[region:ID]]. Empty when the publication has not been processed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["figure", "table", "equation"],
                        "description": "Only this kind. Omit for all.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SEARCH_PARAGRAPHS,
            "description": (
                "Find passages of the publication by meaning, not by words: returns the best matching "
                "paragraphs with their page, section and a relevance score. Prefer it over "
                f"`{SEARCH_PDF}` for questions and topics; use `{SEARCH_PDF}` for an exact term or name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for, in a sentence or a few words."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": GET_REGION,
            "description": (
                "Read one figure, table or equation by the id `list_page_regions` returned: a figure's "
                "caption and category, a table's content as markdown, an equation's LaTeX."
            ),
            "parameters": {
                "type": "object",
                "properties": {"region_id": {"type": "string", "description": "Region id."}},
                "required": ["region_id"],
            },
        },
    },
]


@dataclass
class Explanation:
    simple: str
    examples: List[dict] = field(default_factory=list)
    #: Pages the model read or was shown, for the reader to check.
    pages: List[int] = field(default_factory=list)
    tokens_used: int = 0


def explain(
    document: ReaderDocument,
    *,
    text: str,
    page: Optional[int],
    title: str,
    authors: str,
    language: Optional[str] = None,
) -> Explanation:
    client = OllamaClient()
    pages_read: List[int] = []

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _opening(document, text, page, title, authors, language, pages_read)},
    ]
    tokens = 0

    for _round in range(MAX_TOOL_ROUNDS):
        content, tool_calls, used = _complete(client, messages, TOOL_SPECIFICATIONS)
        tokens += used

        if not tool_calls:
            if structured.parse_object(content, _valid) is None:
                # Tools were offered, so `format` was not: once, ask again constrained to the schema (IP-024).
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": f"{structured.RETRY_INSTRUCTION} {_language_instruction(language)}"}
                )
                retry, _tool_calls, used = _complete(client, messages, None, ANSWER_SCHEMA)
                tokens += used
                if structured.parse_object(retry, _valid) is not None:
                    content = retry
            return _parse(content, pages_read, tokens)

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            messages.append({"role": "tool", "content": run_document_tool(document, call, pages_read)})

    # Out of rounds: ask for the answer from what has been read so far.
    messages.append(
        {
            "role": "user",
            "content": f"Stop reading now and answer with the JSON object. {_language_instruction(language)}",
        }
    )
    content, _tool_calls, used = _complete(client, messages, None, ANSWER_SCHEMA)
    return _parse(content, pages_read, tokens + used)


def _opening(document, text, page, title, authors, language, pages_read) -> str:
    parts = [f'Publication: "{title}"' + (f" by {authors}" if authors else "")]
    parts.append(f"It has {document.page_count} pages.")

    if page is not None and document.page(page) is not None:
        section = document.section_of(page)
        if section:
            parts.append(f"The passage is on page {page}, in: {' › '.join(section)}.")
        parts.append(f"Text of page {page}:\n{_clip(document.page(page))}")
        pages_read.append(page)
    else:
        parts.append(f"The page of the passage is unknown; use `{SEARCH_PDF}` to find it.")

    parts.append(f'The reader selected:\n"""\n{text}\n"""')
    parts.append(_language_instruction(language))
    return "\n\n".join(parts)


LANGUAGE_NAMES = prompts.LANGUAGE_NAMES


def _language_instruction(language: Optional[str]) -> str:
    """Pins the answer to the reader's UI language, whatever the book is written in.

    A bare code is not enough: asked for `sk`, small models drift into Czech, so
    the language is named and Czech is ruled out explicitly.
    """
    name = prompts.language_name(language)
    if name is None:
        return "Write the explanation in the language of the selected passage."
    return (
        f"Write `simple` and every example in {name}, even when the publication is in another language."
        + prompts.not_czech(language)
    )


#: The explanation's shape, for Ollama's `format` when no tools are offered (IP-022): the
#: answer is constrained to it rather than parsed out of free text.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "simple": {"type": "string"},
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                "required": ["label", "description"],
            },
        },
    },
    "required": ["simple", "examples"],
}


def _complete(client: OllamaClient, messages: List[dict], specifications: Optional[List[dict]], response_format=None):
    content = ""
    tool_calls: List[dict] = []
    tokens = 0
    # A constrained format and tools do not mix: with tools offered the model must
    # be free to emit a call instead of the JSON answer.
    if specifications:
        response_format = None
    for delta in client.chat(messages, tools=specifications, response_format=response_format):
        content += delta.content
        tool_calls.extend(delta.tool_calls)
        if delta.done:
            tokens = delta.total_tokens
    return content, tool_calls, tokens


def run_document_tool(document: ReaderDocument, call: dict, pages_read: List[int]) -> str:
    """Run `read_pdf_page` or `search_pdf`; also used by chat turns bound to a reader's copy.

    `pages_read` is the request's reading so far. Distinct pages are capped by
    `EVILFLOWERS_ASSISTANT_MAX_PAGES_READ` (IP-018 Q5): a page already read is
    free to read again, a new one past the budget is refused so the model answers
    from what it has. Searching does not count.
    """
    budget = settings.EVILFLOWERS_ASSISTANT_MAX_PAGES_READ
    function = call.get("function") or {}
    name = function.get("name") or ""
    arguments = function.get("arguments") or {}

    # Same leniency as the chat loop: a model can emit arguments as a string.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}

    try:
        if name == READ_PDF_PAGE:
            start = int(arguments.get("page"))
            count = max(1, min(int(arguments.get("count") or 1), MAX_PAGES_PER_READ))
            pages = []
            over_budget = False
            for number in range(start, start + count):
                page_text = document.page(number)
                if page_text is None:
                    break
                if number not in pages_read:
                    if len(pages_read) >= budget:
                        over_budget = True
                        break
                    pages_read.append(number)
                pages.append({"page": number, "text": _clip(page_text)})
            if over_budget and not pages:
                return json.dumps(
                    {
                        "error": f"Reading budget of {budget} pages reached. Answer from the pages you have read, "
                        "and say if something you would need is on a page you could not read."
                    }
                )
            if not pages:
                return json.dumps({"error": f"No such page. The publication has {document.page_count} pages."})
            return json.dumps({"pages": pages}, ensure_ascii=False)

        if name == SEARCH_PDF:
            query = str(arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"error": "`query` is required."})
            if arguments.get("regex") is True:
                try:
                    return json.dumps({"results": document.search_regex(query)}, ensure_ascii=False)
                except ValueError as error:
                    return json.dumps({"error": str(error)})
            return json.dumps({"results": document.search(query)}, ensure_ascii=False)

        if name in (LIST_PAGE_REGIONS, GET_REGION, LIST_REGIONS):
            return _run_region_tool(document, name, arguments)

        if name == SEARCH_PARAGRAPHS:
            return _search_paragraphs(document, str(arguments.get("query") or "").strip())
    except (TypeError, ValueError):
        return json.dumps({"error": f"Invalid arguments for `{name}`."})

    return json.dumps({"error": f"Unknown tool `{name}`."})


def _run_region_tool(document: ReaderDocument, name: str, arguments: dict) -> str:
    """Regions of the reader's copy (IP-018 Phase 5); reading them costs no page budget."""
    extraction = _extraction(document)
    if extraction is None:
        return json.dumps({"error": "This publication has no extracted figures, tables or equations yet."})

    if name == LIST_PAGE_REGIONS:
        return json.dumps({"regions": extraction.page_regions(int(arguments.get("page")))}, ensure_ascii=False)

    if name == LIST_REGIONS:
        kind = arguments.get("kind") if arguments.get("kind") in ("figure", "table", "equation") else None
        return json.dumps({"regions": extraction.all_regions(kind)}, ensure_ascii=False)

    try:
        region = extraction.region(uuid.UUID(str(arguments.get("region_id"))))
    except ValueError:
        region = None
    if region is None:
        return json.dumps({"error": "No such region in this publication."})
    return json.dumps({"region": region}, ensure_ascii=False)


def _extraction(document: ReaderDocument):
    from apps.extraction.reader import ReaderExtraction

    if not hasattr(document, "_extraction"):
        document._extraction = ReaderExtraction.for_document(document)
    return document._extraction


def _search_paragraphs(document: ReaderDocument, query: str) -> str:
    """Paragraphs by meaning over the paragraph index (IP-018 Phase 8); `search_pdf` without one."""
    from apps.extraction.reader import EmbeddingUnavailable

    if not query:
        return json.dumps({"error": "`query` is required."})

    extraction = _extraction(document)
    if extraction is not None and extraction.has_paragraphs():
        try:
            vector = extraction.embed(query)
        except EmbeddingUnavailable:
            vector = None
        if vector is not None:
            candidates = extraction.search_paragraphs(
                vector, settings.EVILFLOWERS_ASSISTANT_PARAGRAPH_MIN_SCORE, MAX_PARAGRAPH_RESULTS
            )
            best = sorted(candidates, key=lambda c: -c.score)[:MAX_PARAGRAPH_RESULTS]
            paragraphs = [
                {
                    "page": candidate.page,
                    "score": candidate.score,
                    "section": candidate.paragraph.section,
                    "text": _clip_to(candidate.paragraph.text, MAX_PARAGRAPH_CHARS),
                }
                for candidate in sorted(best, key=lambda c: c.paragraph.seq)
            ]
            return json.dumps({"paragraphs": paragraphs}, ensure_ascii=False)

    # No index or no embedding: the word search, said so, so the model can adapt its queries.
    return json.dumps(
        {
            "note": "Search by meaning is not available for this publication; these are word matches.",
            "results": document.search(query),
        },
        ensure_ascii=False,
    )


def _clip_to(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " […]"


def _clip(text: str) -> str:
    if len(text) <= MAX_PAGE_CHARS:
        return text
    return text[:MAX_PAGE_CHARS] + " […]"


def _valid(data: dict) -> bool:
    return isinstance(data.get("simple"), str)


def _parse(content: str, pages_read: List[int], tokens: int) -> Explanation:
    """The model's JSON answer, or its prose when it did not follow the format."""
    data = structured.parse_object(content, _valid)
    if data is not None:
        examples = [
            {"label": str(example.get("label") or ""), "description": str(example.get("description") or "")}
            for example in data.get("examples") or []
            if isinstance(example, dict)
        ]
        return Explanation(simple=data["simple"].strip(), examples=examples, pages=pages_read, tokens_used=tokens)

    logger.warning("Explain answer was not the expected JSON: %r", (content or "")[:200])
    return Explanation(simple=(content or "").strip(), pages=pages_read, tokens_used=tokens)

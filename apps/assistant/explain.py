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
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from django.conf import settings

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

SYSTEM_PROMPT = (
    "You explain passages from a publication to a reader who selected them in a PDF viewer. "
    "The reader is in the middle of reading and wants to understand what the passage means in "
    "context — what it refers to, what the terms mean and why it matters at this point.\n\n"
    "You are given the page the passage is on. When it refers to something defined elsewhere "
    f'("this method", "the theorem above", an abbreviation), find it: use `{SEARCH_PDF}` to '
    f"locate where a term is introduced and `{READ_PDF_PAGE}` to read earlier pages. Read only "
    "what you need. Base the explanation on what the book says; never invent its content.\n\n"
    "When you are done reading, answer with a single JSON object and nothing else:\n"
    '{"simple": "<a short plain-language explanation, 2-5 sentences>", '
    '"examples": [{"label": "<short title>", "description": "<a concrete example or analogy>"}]}\n'
    "Give 1-3 examples. Do not mention page numbers: the viewer's numbering differs from the "
    "numbers printed in the book, and small raised numbers in the text are footnote markers."
)

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
                "Search the publication's full text. Returns the best matching pages with a snippet "
                "around the match. Use it to find where a term, name or method is introduced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words or a phrase to look for."},
                },
                "required": ["query"],
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
    content, _tool_calls, used = _complete(client, messages, None)
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


#: UI languages the reader can switch between, by code.
LANGUAGE_NAMES = {"sk": "Slovak", "en": "English"}


def _language_instruction(language: Optional[str]) -> str:
    """Pins the answer to the reader's UI language, whatever the book is written in.

    A bare code is not enough: asked for `sk`, small models drift into Czech, so
    the language is named and Czech is ruled out explicitly.
    """
    code = (language or "").split("-")[0].lower()
    name = LANGUAGE_NAMES.get(code)
    if name is None:
        return "Write the explanation in the language of the selected passage."

    instruction = f"Write `simple` and every example in {name}, even when the publication is in another language."
    if code == "sk":
        instruction += " It must be Slovak, never Czech."
    return instruction


def _complete(client: OllamaClient, messages: List[dict], specifications: Optional[List[dict]]):
    content = ""
    tool_calls: List[dict] = []
    tokens = 0
    for delta in client.chat(messages, tools=specifications):
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
            return json.dumps({"results": document.search(query)}, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"error": f"Invalid arguments for `{name}`."})

    return json.dumps({"error": f"Unknown tool `{name}`."})


def _clip(text: str) -> str:
    if len(text) <= MAX_PAGE_CHARS:
        return text
    return text[:MAX_PAGE_CHARS] + " […]"


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _parse(content: str, pages_read: List[int], tokens: int) -> Explanation:
    """The model's JSON answer, or its prose when it did not follow the format."""
    match = _JSON_OBJECT.search(content or "")
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict) and isinstance(data.get("simple"), str):
            examples = [
                {"label": str(example.get("label") or ""), "description": str(example.get("description") or "")}
                for example in data.get("examples") or []
                if isinstance(example, dict)
            ]
            return Explanation(simple=data["simple"].strip(), examples=examples, pages=pages_read, tokens_used=tokens)

    logger.warning("Explain answer was not the expected JSON: %r", (content or "")[:200])
    return Explanation(simple=(content or "").strip(), pages=pages_read, tokens_used=tokens)

"""What a tool call is about and what it found, in words a reader can follow (IP-022 §4).

The chat shows the turn's tool calls as a compact log, the way a coding agent shows the files it
searched and read: "Searched the catalog · *machine learning* · 6 results", expandable to the
titles it found. Two SSE frames carry it:

- `status` (`state: "tool"`) before the call, with `describe()`: the query, the pages or the
  request the model asked for;
- `activity` after the call, with `summarize()`: the same plus how many results came back and
  the publications (`id` + `title`) or pages they were.

Only readable values go out. Ids travel with a title so the client can link a publication, never
as the only label. Nothing here is stored: a reloaded chat shows no log (the issue's "nothing is
stored"), and the tool rows themselves are unchanged.
"""

import json
from typing import Iterable, List, Optional

#: Publications listed under one call at most; the count still says how many there were.
MAX_ENTRIES = 10
#: Characters of a query or request echoed back.
MAX_TEXT = 160


def _text(value) -> Optional[str]:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe(name: str, arguments: dict) -> dict:
    """What the call asks for, from its arguments alone (sent before it runs)."""
    arguments = arguments if isinstance(arguments, dict) else {}
    detail = {}
    query = _text(arguments.get("query"))
    if query:
        detail["query"] = query
    prompt = _text(arguments.get("prompt"))
    if prompt:
        detail["query"] = prompt
    if name == "read_pdf_page":
        start = _int(arguments.get("page"))
        if start is not None:
            count = max(1, min(_int(arguments.get("count")) or 1, 20))
            detail["pages"] = list(range(start, start + count))
    elif name == "list_page_regions":
        page = _int(arguments.get("page"))
        if page is not None:
            detail["pages"] = [page]
    return detail


def _entry(entry_id, title) -> Optional[dict]:
    title = _text(title)
    if not title:
        return None
    return {"id": str(entry_id), "title": title} if entry_id else {"title": title}


def _entries(records: Iterable[Optional[dict]]) -> List[dict]:
    seen, entries = set(), []
    for record in records:
        if record is None:
            continue
        key = record.get("id") or record["title"]
        if key in seen:
            continue
        seen.add(key)
        entries.append(record)
    return entries


def _catalog_entries(name: str, payload: dict) -> List[dict]:
    items = payload.get("items") or []
    if name == "get_entry":
        entry = payload.get("entry") or {}
        return _entries([_entry(entry.get("id"), entry.get("title"))])
    if name == "get_my_shelf":
        return _entries(
            _entry((item.get("entry") or {}).get("id"), (item.get("entry") or {}).get("title")) for item in items
        )
    if name == "list_my_loans":
        return _entries(_entry(item.get("entry_id"), item.get("entry_title")) for item in items)
    if name == "list_categories":
        return []
    return _entries(_entry(item.get("id") or item.get("entry_id"), item.get("title")) for item in items)


def from_history(messages) -> list:
    """The visible messages of a chat, each answer carrying the `activity` log of its turn.

    `messages` are all rows of a chat in order, tool traffic included. An assistant row with `tool_calls`
    is followed by one tool row per call, in order (`turn._run_tool` writes them so); the log is rebuilt from
    those pairs, the same way the live `activity` frames were built, and set on the answer closing the turn.
    `displayBooks` and repeated calls are left out, as they were live.
    """
    from apps.assistant.tools import is_display_books
    from apps.assistant.turn import REPEATED_CALL

    visible, pending, calls = [], [], []
    for message in messages:
        if message.role == "user":
            pending, calls = [], []
        elif message.role == "assistant" and message.tool_calls:
            calls.extend(message.tool_calls)
        elif message.role == "tool":
            call = calls.pop(0) if calls else None
            if call is None:
                continue
            function = call.get("function") or {}
            name = function.get("name") or ""
            if not name or is_display_books(name) or message.text == REPEATED_CALL:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    arguments = {}
            arguments = arguments if isinstance(arguments, dict) else {}
            summary = summarize(name, arguments, message.tool_result, message.text or "")
            pending.append({**summary, "id": f"{message.pk}"})

        if message.is_visible:
            if message.role == "assistant":
                message.activity = pending or None
                pending = []
            visible.append(message)
    return visible


def summarize(
    name: str,
    arguments: dict,
    payload=None,
    text: str = "",
    pages: Optional[List[int]] = None,
) -> dict:
    """The `activity` frame for a finished call.

    `payload` is the full result of a catalog tool, `text` what the model was given (the only result
    of the document tools), `pages` the pages a `read_pdf_page` call actually read.
    """
    summary = {"tool": name, **describe(name, arguments)}
    try:
        result = json.loads(text) if text else None
    except (TypeError, ValueError):
        result = None
    if isinstance(result, dict) and result.get("error"):
        summary["error"] = True
        return summary

    if isinstance(payload, dict):
        entries = _catalog_entries(name, payload)
        # A record read is its title, not "1 result".
        if name != "get_entry":
            items = payload.get("items") or []
            summary["count"] = (payload.get("metadata") or {}).get("total", len(items)) if items else 0
        if entries:
            summary["entries"] = entries[:MAX_ENTRIES]
        return summary

    if not isinstance(result, dict):
        return summary
    if name == "read_pdf_page":
        read = [page.get("page") for page in result.get("pages") or [] if isinstance(page, dict)]
        # The pages are the result; no count next to them.
        summary["pages"] = [page for page in read if page is not None] or (pages or [])
    elif "results" in result or "paragraphs" in result:
        # A search reads as "patch · 12 results"; the pages it matched are noise in the log.
        hits = result.get("paragraphs") or result.get("results") or []
        summary["count"] = len(hits)
    elif "regions" in result:
        summary["count"] = len(result.get("regions") or [])
    elif "region" in result:
        summary["count"] = 1
    elif "annotation_id" in result:
        summary["count"] = 1
    return summary

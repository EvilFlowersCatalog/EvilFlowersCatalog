"""A bound chat's tools over the reader's copy, with evidence labels (IP-030 §2, Q1 and Q6).

Explain and highlight keep `explain.run_document_tool` as it is: they answer in JSON, not in cited prose. A chat
bound to the reader's copy cites like every other chat, so its document tools label what they return:

- `read_pdf_page` → each page an `E#` (kind `page`);
- `search_paragraphs` → each paragraph an `E#` with its paragraph id, so the citation lands on the paragraph and
  its best sentence, not only the page;
- `list_regions` → each figure, table or equation an `F#`; `get_region` reads one by its label.

`search_pdf` (regular expressions) and `list_page_regions` are not offered in chats any more (Q6: swapped for
`follow`); `[[page:N]]` and `[[region:…]]` are no longer asked for, and old messages keep rendering in the portal.
"""

import json
from typing import Optional, Tuple

from django.conf import settings

from apps.assistant import evidence, explain
from apps.assistant.library import label_units

#: Tools a bound chat runs against its copy, in this module.
TOOLS = (explain.READ_PDF_PAGE, explain.SEARCH_PARAGRAPHS, explain.LIST_REGIONS, explain.GET_REGION)

SPECIFICATIONS = [
    {
        "type": "function",
        "function": {
            "name": explain.READ_PDF_PAGE,
            "description": (
                "Read pages of this publication, numbered as the reader's viewer numbers them (at most "
                f"{explain.MAX_PAGES_PER_READ} per call); each page has a label E#."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "First page, 1-based."},
                    "count": {"type": "integer", "description": f"1-{explain.MAX_PAGES_PER_READ}"},
                },
                "required": ["page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": explain.SEARCH_PARAGRAPHS,
            "description": "Find paragraphs of this publication by meaning: page, section, label E#.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look for, in a sentence."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": explain.LIST_REGIONS,
            "description": "Every figure, table and equation of this publication, in reading order, each with a label F#.",
            "parameters": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["figure", "table", "equation"]}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": explain.GET_REGION,
            "description": "Read one figure, table or equation by its label: caption, a table as markdown, LaTeX.",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string", "description": "F# from list_regions."}},
                "required": ["label"],
            },
        },
    },
]


def _arguments(call: dict) -> dict:
    arguments = (call.get("function") or {}).get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return arguments if isinstance(arguments, dict) else {}


def run(document, call: dict, pages_read, budget: Optional[int], ledger) -> Tuple[Optional[dict], str]:
    """`(full payload or None, text the model sees)` of one document tool call in a bound chat."""
    name = (call.get("function") or {}).get("name") or ""
    arguments = _arguments(call)
    entry = document.acquisition.entry if getattr(document, "acquisition", None) is not None else None

    if name == explain.READ_PDF_PAGE:
        result = json.loads(explain.run_document_tool(document, call, pages_read, budget))
        if "error" in result or entry is None:
            return None, json.dumps(result, ensure_ascii=False)
        payload = {"entry_id": str(entry.pk), "title": entry.title, "pages": result.get("pages") or []}
        evidence.label_pages(payload, ledger)
        return payload, json.dumps({"pages": payload["pages"]}, ensure_ascii=False)

    if name == explain.SEARCH_PARAGRAPHS:
        return _search(document, entry, str(arguments.get("query") or "").strip(), ledger)

    if name in (explain.LIST_REGIONS, explain.GET_REGION):
        return _regions(document, entry, name, arguments, ledger)

    return None, explain.run_document_tool(document, call, pages_read, budget)


def _search(document, entry, query: str, ledger) -> Tuple[Optional[dict], str]:
    from apps.extraction.reader import EmbeddingUnavailable

    if not query:
        return None, json.dumps({"error": "`query` is required."})
    extraction = explain._extraction(document)
    if entry is not None and extraction is not None and extraction.has_paragraphs():
        try:
            vector = extraction.embed(query)
        except EmbeddingUnavailable:
            vector = None
        if vector is not None:
            candidates = extraction.search_paragraphs(
                vector, settings.EVILFLOWERS_ASSISTANT_PARAGRAPH_MIN_SCORE, explain.MAX_PARAGRAPH_RESULTS
            )
            best = sorted(candidates, key=lambda c: -c.score)[: explain.MAX_PARAGRAPH_RESULTS]
            passages = []
            for candidate in sorted(best, key=lambda c: c.paragraph.seq):
                paragraph = candidate.paragraph
                passages.append(
                    {
                        "kind": evidence.PASSAGE,
                        "entry_id": str(entry.pk),
                        "title": entry.title,
                        "page": candidate.page,
                        "source_page": paragraph.page if paragraph.page != candidate.page else None,
                        "section": paragraph.section,
                        "paragraph_id": str(paragraph.pk),
                        "text": explain._clip_to(paragraph.text, explain.MAX_PARAGRAPH_CHARS),
                        "score": candidate.score,
                    }
                )
            payload = {"paragraphs": passages}
            issued = label_units(ledger, passages)
            if issued:
                payload[evidence.EVIDENCE_KEY] = issued
            seen = [
                {key: passage[key] for key in ("label", "page", "section", "score", "text") if passage.get(key)}
                for passage in passages
            ]
            return payload, json.dumps({"paragraphs": seen}, ensure_ascii=False)

    # No index or no embedding: the word search, said so, so the model can adapt its queries.
    return None, json.dumps(
        {
            "note": "Search by meaning is not available for this publication; these are word matches by page. "
            "Read the best pages with read_pdf_page before citing.",
            "results": document.search(query),
        },
        ensure_ascii=False,
    )


def _regions(document, entry, name: str, arguments: dict, ledger) -> Tuple[Optional[dict], str]:
    extraction = explain._extraction(document)
    if extraction is None or entry is None:
        return None, json.dumps({"error": "This publication has no extracted figures, tables or equations yet."})

    if name == explain.LIST_REGIONS:
        kind = arguments.get("kind") if arguments.get("kind") in ("figure", "table", "equation") else None
        figures = []
        for line in extraction.all_regions(kind):
            figures.append(
                {
                    "kind": line["kind"],
                    "entry_id": str(entry.pk),
                    "title": entry.title,
                    "page": line["page"],
                    "region_id": line["id"],
                    "caption": line.get("preview") or None,
                    "description": line.get("description"),
                }
            )
        payload = {"figures": figures}
        issued = label_units(ledger, figures)
        if issued:
            payload[evidence.EVIDENCE_KEY] = issued
        seen = [
            {key: figure[key] for key in ("label", "kind", "page", "caption", "description") if figure.get(key)}
            for figure in figures
        ]
        return payload, json.dumps({"regions": seen}, ensure_ascii=False)

    label = evidence.normalise(str(arguments.get("label") or arguments.get("region_id") or ""))
    source = ledger.sources.get(label) if label else None
    region = extraction.region(source["region_id"]) if source and source.get("region_id") else None
    if region is None:
        return None, json.dumps({"error": "No such figure, table or equation; use a label from list_regions."})
    region.pop("id", None)
    return None, json.dumps(
        {"label": label, **{k: v for k, v in region.items() if v not in (None, "")}}, ensure_ascii=False
    )

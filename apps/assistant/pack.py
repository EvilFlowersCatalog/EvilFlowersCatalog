"""Evidence first: the pack gathered before the model's first round (IP-030 §1).

Today the model picks the retrieval, and a content question costs four generated tool calls on an 8 tok/s CPU
before the answer. The turn instead runs the retrieval itself, before round 1:

| Channel | What | In the pack |
|---|---|---|
| Catalog | `search_entries` (hybrid, IP-021) on the message | ≤ `PACK_ENTRIES` slim records |
| Passages | `discovery.search_passages`; scoped to the chat's publication when bound or attached | ≤ `PACK_PASSAGES` × ≤ 600 characters |
| Figures | `regions.search_regions`, plus figures the chosen passages refer to | ≤ `PACK_FIGURES`, caption only |

The channels share one query embedding (`embeddings.embed_query` caches it), and the database work is three
indexed queries; they run one after another, which keeps one database connection per turn under gevent.

A passage or figure below `EVILFLOWERS_ASSISTANT_PACK_MIN_SCORE` (cosine) stays out; an empty pack says so in a
short note, so the model does not pad. The pack is handed to the model as the result of a `gather_evidence` call
it "already made" (`turn._prefetch`): stored as an assistant row with that tool call and a tool row with the
result, so the replayed history stays append-only (#138). The same tool is on offer, so a model that calls it
itself gets a pack rather than an error.

No pack is gathered for a greeting, a question about the assistant itself, or a short follow-up about what was
just shown (`needs_pack`).
"""

import re
from typing import List, Optional

from django.conf import settings

from apps.assistant.library import PASSAGE_CHARS, ToolContext, label_units

GATHER_EVIDENCE = "gather_evidence"

SPECIFICATION = (
    "Gather evidence for a question at once: matching publications, passages (E#) and figures (F#).",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The question, in a sentence."}},
        "required": ["query"],
    },
)

#: Characters of a message that is a greeting or acknowledgement at most, when it matches `_SMALL_TALK`.
SHORT = 40

_SMALL_TALK = re.compile(
    r"^\s*(ahoj|čau|cau|dobr[ýy] (deň|den|večer|ráno)|zdrav[ií]m|hello|hi|hey|thanks?|thank you|ďakujem|dakujem|"
    r"vďaka|vdaka|ok|okay|dobre|super|fajn|áno|ano|nie|yes|no)\b[\s!.?,]*$",
    re.IGNORECASE,
)
_CAPABILITY = re.compile(
    r"(čo (všetko )?(vieš|dokážeš|umíš|vies|dokazes)|co vies|what can you do|what are you|who are you|"
    r"showcase|kto si|čo si|ako (ti|vám) (mám|môžem)|how do you work|tvoje (schopnosti|možnosti))",
    re.IGNORECASE,
)


def needs_pack(text: str) -> bool:
    """Whether a message is worth gathering evidence for (greetings and capability questions are not)."""
    text = (text or "").strip()
    if len(text) < 3:
        return False
    if len(text) <= SHORT and _SMALL_TALK.match(text):
        return False
    if _CAPABILITY.search(text):
        return False
    return True


def _scope(context: ToolContext, entry=None) -> Optional[dict]:
    """Passages from the chat's publication when it is bound to one or the message attaches one."""
    if context.bound_entry_id:
        return {"id": context.bound_entry_id}
    if entry is not None:
        return {"id": str(entry.pk)}
    chat_entry = getattr(context.chat, "entry_id", None)
    if chat_entry:
        return {"id": str(chat_entry)}
    return None


def gather(context: ToolContext, query: str, entry=None) -> dict:
    """The pack for `query`, labelled into the context's ledger (full payload; `slim` is what the model sees)."""
    from apps.assistant import tools

    query = " ".join((query or "").split())[:500]
    floor = settings.EVILFLOWERS_ASSISTANT_PACK_MIN_SCORE
    scope = _scope(context, entry)

    publications = _publications(context, query, scope)
    passages = _passages(context, query, scope, floor)
    figures = _figures(context, query, scope, floor, passages)

    payload = {"query": query, "publications": publications, "passages": passages, "figures": figures}
    issued = label_units(context.ledger, passages + figures) if context.ledger is not None else {}
    if issued:
        payload["evidence"] = issued
    if not (passages or figures):
        payload["low_relevance"] = True
        payload["note"] = (
            f"Nothing in the publications' text matched '{query[:80]}' closely. Answer from the publications "
            f"found, or search with other words ({tools.SEARCH_PASSAGES})."
            if publications
            else f"No new evidence was found for '{query[:80]}'. Answer from earlier results in this chat if they "
            "answer it; otherwise say so in one sentence and suggest other words."
        )
    return payload


def _publications(context: ToolContext, query: str, scope) -> List[dict]:
    from apps.assistant import tools
    from apps.mcp.errors import ToolError

    if scope:
        # The chat is about one publication: its record is already in the conversation.
        return []
    try:
        payload = tools.call(
            context.request, "search_entries", {"query": query, "limit": settings.EVILFLOWERS_ASSISTANT_PACK_ENTRIES}
        )
    except (ToolError, tools.UnknownTool, tools.ToolNotPermitted):
        return []
    return [tools._slim_entry(item) for item in payload.get("items") or []]


def _passages(context: ToolContext, query: str, scope, floor: float) -> List[dict]:
    from apps.extraction.discovery import search_passages

    limit = settings.EVILFLOWERS_ASSISTANT_PACK_PASSAGES
    result = search_passages(context.request, query, limit=limit, per_entry=2, scope=scope or {})
    titles = _titles(context, [hit.entry_id for hit in result.items])
    page_of = _page_mapping(context)
    passages = []
    for hit in result.items:
        for passage in hit.passages:
            if not passage.text:
                # Readium LCP or IP-blocked: nothing to cite.
                continue
            # By meaning, above the floor. With search by meaning down (`degraded`) the word matches are all
            # there is; their rank is no cosine, so no floor applies.
            if not result.degraded and ("semantic" not in passage.matched or passage.score < floor):
                continue
            page = page_of(str(hit.entry_id), passage.page)
            if page is None:
                continue
            passages.append(
                {
                    "kind": "passage",
                    "entry_id": str(hit.entry_id),
                    "title": titles.get(str(hit.entry_id)),
                    "page": page,
                    "source_page": passage.page if page != passage.page else None,
                    "section": passage.section,
                    "paragraph_id": str(passage.paragraph_id),
                    "text": passage.text[:PASSAGE_CHARS],
                    "score": passage.score,
                }
            )
            if len(passages) >= limit:
                return passages
    return passages


def _figures(context: ToolContext, query: str, scope, floor: float, passages: List[dict]) -> List[dict]:
    from apps.extraction.models import Reference
    from apps.extraction.regions import search_regions

    limit = settings.EVILFLOWERS_ASSISTANT_PACK_FIGURES
    page_of = _page_mapping(context)
    figures, seen = [], set()

    # Figures the chosen passages refer to first: they illustrate exactly what will be cited.
    referred = (
        Reference.objects.filter(
            paragraph_id__in=[passage["paragraph_id"] for passage in passages], region__isnull=False
        )
        .select_related("region__run__acquisition")
        .order_by("paragraph__seq")
    )
    titles = {passage["entry_id"]: passage["title"] for passage in passages}
    for reference in referred:
        region = reference.region
        entry_id = str(region.run.acquisition.entry_id)
        if region.pk in seen:
            continue
        page = page_of(entry_id, region.page)
        if page is None:
            continue
        seen.add(region.pk)
        figures.append(
            _figure(entry_id, titles.get(entry_id), region.kind, page, region.page, region.pk, _caption(region))
        )
        if len(figures) >= limit:
            return figures

    hits = search_regions(context.request, query, limit=limit, scope=scope, min_score=floor)
    hit_titles = _titles(context, [hit.entry_id for hit in hits])
    for hit in hits:
        if hit.region_id in {str(pk) for pk in seen}:
            continue
        page = page_of(hit.entry_id, hit.page)
        if page is None:
            continue
        figures.append(
            _figure(hit.entry_id, hit_titles.get(hit.entry_id), hit.kind, page, hit.page, hit.region_id, hit.caption)
        )
        if len(figures) >= limit:
            break
    return figures


def _figure(entry_id, title, kind, page, source_page, region_id, caption) -> dict:
    return {
        "kind": kind,
        "entry_id": str(entry_id),
        "title": title,
        "page": page,
        "source_page": source_page if page != source_page else None,
        "region_id": str(region_id),
        "caption": (caption or "")[:300] or None,
    }


def _caption(region) -> Optional[str]:
    return region.printed_caption or region.caption or region.description


def _titles(context: ToolContext, entry_ids) -> dict:
    from apps.core.models import Entry

    return {str(pk): title for pk, title in Entry.objects.filter(pk__in=list(entry_ids)).values_list("pk", "title")}


def _page_mapping(context: ToolContext):
    """Source page → the page the reader sees: the viewer's numbering for the bound copy, else unchanged."""
    bound = context.bound_entry_id
    reader_page = None
    if bound:
        sources = getattr(context.document, "source_pages", None) or []
        reader_page = {source: index + 1 for index, source in enumerate(sources) if source}

    def page_of(entry_id: str, page: int) -> Optional[int]:
        if bound and entry_id == bound and reader_page is not None:
            return reader_page.get(page)
        return page

    return page_of


def slim(payload: dict) -> dict:
    """The pack as the model sees it: labelled evidence without ids it would only copy, scores rounded away."""
    from apps.assistant.evidence import strip

    seen = strip(payload, private=("paragraph_id", "region_id", "source_page", "score", "kind"))
    for figure in seen.get("figures") or []:
        if figure.get("kind") is None:
            figure.pop("kind", None)
    return {key: value for key, value in seen.items() if value not in (None, [], {})}


def sources(payload: dict, request) -> dict:
    """The `evidence` frame: the pack's sources, completed for the portal's evidence panel (§ API)."""
    from apps.assistant.citations import complete

    items = []
    ledger_sources = payload.get("evidence") or {}
    for unit in (payload.get("passages") or []) + (payload.get("figures") or []):
        label = unit.get("label")
        source = ledger_sources.get(label)
        if not label or source is None:
            continue
        item = complete(label, source, payload.get("query") or "", request)
        if unit.get("score") is not None:
            item["score"] = unit["score"]
        items.append(item)
    return {"sources": items, "low_relevance": bool(payload.get("low_relevance"))}

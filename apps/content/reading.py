"""Graded, citable reads of a publication's content (#176): what the MCP content tools return.

Every function resolves the entry through the catalog ACL first (a hidden entry is not found), asks the gate
(`access.decide`) and returns no more than the tier allows, with a receipt (`access`). Text is addressed by
locators (#171), so whatever an agent keeps is an address that the next reader resolves under their own access.
What is handed out is charged and logged through `apps.content.disclosure`.

| Function | Needs |
|---|---|
| `outline` | `outline` for headings; `snippet` for captions |
| `read` | `passage` (at most `PASSAGE_MAX_CHARS` a call); `full` reads up to `FULL_MAX_CHARS` |
| `region` | `snippet` for the caption and crop, `passage` for a table's Markdown and an equation's LaTeX |
| `search` | `snippet` for a hit's text; every visible hit is listed with its address either way |
| `pack` | `passage` per passage |
| `citation` | `snippet` for the quote |
"""

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence
from uuid import UUID

from django.http import QueryDict
from django.urls import reverse

from apps.content import access, citation, disclosure
from apps.content.access import Surface, Tier

#: Characters `read` returns at most per call at tier `passage`, and at tier `full`.
PASSAGE_MAX_CHARS = 2000
FULL_MAX_CHARS = 12000
DEFAULT_MAX_CHARS = 2000
#: Paragraphs before the addressed one `read` may include.
MAX_CONTEXT = 3
#: Characters of a search hit's text.
SNIPPET_CHARS = 600
#: Default and largest `content_pack` budgets.
DEFAULT_PACK_CHARS = 6000
MAX_PACK_CHARS = 20000
MAX_PACK_PASSAGES = 12

KINDS = ("passage", "figure", "table")

_CITE_URL = re.compile(r"/cite/(?P<entry>[0-9a-fA-F-]{36})/(?P<key>[a-z0-9]{8,40})/?(?:[?#].*)?$")


class NotFound(Exception):
    def __init__(self, kind: str, identifier):
        super().__init__(f"{kind} {identifier} not found")
        self.kind = kind
        self.identifier = str(identifier)


class Invalid(ValueError):
    pass


# -- resolution ------------------------------------------------------------------------------------------------


def visible_entry(request, entry_id):
    """The entry if the caller may see it, with what `work()` needs; `NotFound` otherwise."""
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry

    params = QueryDict(mutable=True)
    params["id"] = str(entry_id)
    entry = (
        EntryFilter(params, queryset=Entry.objects.all(), request=request)
        .qs.select_related("catalog", "language")
        .prefetch_related("entry_authors__author")
        .first()
    )
    if entry is None:
        raise NotFound("Entry", entry_id)
    return entry


def decide(request, entry, *, generated: bool = False) -> access.Disclosure:
    return access.decide(request, entry, surface=Surface.MCP, visible=True, generated=generated)


@dataclass
class Address:
    entry_id: str
    key: Optional[str] = None


def parse_address(value: str) -> Address:
    """`evilflowers://entry/<id>[/locator/<key>]` or a `/cite/<id>/<key>` URL."""
    from apps.mcp import uris

    value = (value or "").strip()
    reference = uris.parse(value)
    if reference is not None and reference.kind == uris.ENTRY and reference.part in (None, uris.LOCATOR):
        return Address(reference.identifier, reference.key)
    match = _CITE_URL.search(value)
    # A full link, or the portal's own relative `/cite/<id>/<key>` (what a chat citation's `url` path is, #186).
    if match and value.startswith(("http://", "https://", "/cite/")):
        try:
            return Address(str(UUID(match.group("entry"))), match.group("key"))
        except ValueError:
            pass
    raise Invalid(
        f"'{value}' is not an address: expected evilflowers://entry/<id>/locator/<key>, evilflowers://entry/<id> "
        "or a /cite/<id>/<key> link."
    )


def _resolve(entry, key: str):
    from apps.extraction import locators
    from apps.extraction.models import Locator

    if len(key) > Locator._meta.get_field("key").max_length:
        raise NotFound("Locator", key)
    resolution = locators.resolve(entry.pk, key)
    if resolution is None:
        raise NotFound("Locator", key)
    return resolution


def citation_for(request, value: str) -> dict:
    """The citation object of an address, at the caller's tier."""
    address = parse_address(value)
    if not address.key:
        raise Invalid("A citation needs a locator: evilflowers://entry/<id>/locator/<key> or a /cite link.")
    entry = visible_entry(request, address.entry_id)
    resolution = _resolve(entry, address.key)
    decision = decide(request, entry)
    built = citation.build(request, entry, resolution, decision)
    if "selector" in built:
        disclosure.record(
            request,
            decision,
            [disclosure.Item(chars=len(built["selector"]["exact"]), locator=address.key)],
            charge=False,
        )
    return built


def work_citation(request, entry_id) -> dict:
    """A citation of a whole publication (`evilflowers://entry/<id>`): the work, no page, linked to its detail."""
    from apps.mcp import uris

    entry = visible_entry(request, entry_id)
    return {
        "uri": uris.entry_uri(entry.pk),
        "url": citation.entry_url(entry.pk),
        "kind": "entry",
        "work": citation.work(entry),
        "locator": {},
        "access": citation.access_of(decide(request, entry), entry.pk),
    }


#: Addresses one `cite` call formats at most.
MAX_REFERENCES = 50


def references(request, addresses: List[str], style: str) -> dict:
    """Formatted references of addresses (`cite`, MCP and `POST /api/v1/cite`, #186), each under the caller's access.

    A locator address (or `/cite` link) is cited at its page; a publication address as the whole work. Raises
    `Invalid` for a value that is no address and `NotFound` for a publication or locator the caller cannot see.
    """
    if style not in citation.STYLES:
        raise Invalid(f"`style` must be one of: {', '.join(citation.STYLES)}.")
    objects = []
    for value in list(addresses)[:MAX_REFERENCES]:
        address = parse_address(value)
        objects.append(citation_for(request, value) if address.key else work_citation(request, address.entry_id))
    return {"style": style, "references": citation.cite(objects, style)}


def my_access(request, entry_id) -> dict:
    entry = visible_entry(request, entry_id)
    decision = decide(request, entry)
    payload = {
        "entry_id": str(entry.pk),
        "title": entry.title,
        **citation.access_of(decision, entry.pk),
        "policy_source": decision.policy_source,
        "caps": decision.caps,
        "processor": decision.processor,
        "budget": decision.budget_state.payload() if decision.budget_state is not None else None,
    }
    return payload


# -- outline ---------------------------------------------------------------------------------------------------


def _region_payload(request, entry, region, decision, described: access.Disclosure) -> dict:
    from apps.mcp import uris

    payload = {
        "kind": region.kind,
        "page": region.page,
        "label": citation.label_of(region.printed_caption or region.caption),
    }
    if region.locator_key:
        payload["uri"] = uris.locator_uri(entry.pk, region.locator_key)
        payload["key"] = region.locator_key
    if decision.allows(Tier.SNIPPET) and region.printed_caption:
        payload["caption"] = region.printed_caption
    if described.allows(Tier.SNIPPET) and region.description:
        payload["description"] = region.description
    return payload


def outline(request, entry_id) -> dict:
    from apps.extraction import outline as outlines
    from apps.extraction.locators import active_run, section_keys
    from apps.mcp import uris

    entry = visible_entry(request, entry_id)
    decision = decide(request, entry)
    run = active_run(entry.pk)
    sections = outlines.outline(run)
    keys = section_keys(entry.pk, [section.title for section in sections])
    headings = decision.allows(Tier.OUTLINE)
    payload = {
        "entry_id": str(entry.pk),
        "title": entry.title,
        "processed": run is not None,
        "page_count": (run.page_count if run is not None else None) or entry.page_count,
        "access": citation.access_of(decision, entry.pk),
        "sections": [
            {
                "uri": uris.locator_uri(entry.pk, keys[section.title]),
                "key": keys[section.title],
                # Below `outline` the structure stays and the headings do not.
                "heading": section.title if headings else None,
                "first_page": section.first_page,
                "last_page": section.last_page,
                "paragraphs": section.paragraphs,
            }
            for section in sections
        ],
        "regions": [],
    }
    if run is not None:
        described = decide(request, entry, generated=True)
        payload["regions"] = [
            _region_payload(request, entry, region, decision, described)
            for region in run.regions.visible()
            .order_by("page", "seq")
            .only("id", "kind", "page", "printed_caption", "caption", "description", "locator_key")
        ]
        from apps.extraction import bibliography

        references = bibliography.outline_section(request, entry, run, decision)
        if references is not None:
            payload["references"] = references
    return payload


# -- read ------------------------------------------------------------------------------------------------------


def _start_seq(run, resolution) -> int:
    if resolution is None:
        return 0
    if resolution.paragraph is not None:
        return resolution.paragraph.seq
    if resolution.section is not None:
        return resolution.section.first_seq
    page = resolution.region.page if resolution.region is not None else resolution.source_page
    first = run.paragraphs.filter(page__gte=page).order_by("seq").values_list("seq", flat=True).first()
    return first if first is not None else 0


def _receipt(entry, decision, charged) -> dict:
    """The `access` block of every content tool (`citation.access_of`), with what this call charged (#175)."""
    receipt = citation.access_of(decision, entry.pk)
    if decision.metered:
        receipt["chars_charged"] = charged.chars_charged
        receipt["budget_left"] = charged.budget_left
    return receipt


def _refusal(entry, decision, message: str) -> dict:
    return {
        "entry_id": str(entry.pk),
        "title": entry.title,
        "paragraphs": [],
        "refused": message,
        "access": citation.access_of(decision, entry.pk),
    }


def _unreadable(decision) -> str:
    if decision.reason == access.Reason.BUDGET:
        return "The reading budget of this publication is spent for now; snippets and citations still work."
    if decision.reason == access.Reason.LCP:
        return "This publication is lent through Readium LCP; its text is not available here. Borrow it to read it."
    if decision.reason == access.Reason.OUTSIDE_IP:
        return "This publication may only be read from the library's network."
    if decision.reason == access.Reason.PROCESSOR:
        return "The content policy does not let this text reach an external model provider."
    return "The library's content policy does not let this publication's text be read here."


def read(request, value: str, *, context: int = 0, cursor: Optional[str] = None, max_chars: Optional[int] = None):
    """Paragraphs in reading order from an address, with locators and sentences; whole paragraphs only."""
    from apps.extraction.locators import active_run
    from apps.mcp import uris

    address = parse_address(value)
    entry = visible_entry(request, address.entry_id)
    decision = decide(request, entry)
    if not decision.allows(Tier.PASSAGE):
        return _refusal(entry, decision, _unreadable(decision))
    run = active_run(entry.pk)
    if run is None:
        return _refusal(entry, decision, "This publication has not been processed into paragraphs yet.")

    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            raise Invalid("Invalid cursor: pass back `next_cursor` verbatim.")
    else:
        resolution = _resolve(entry, address.key) if address.key else None
        start = max(0, _start_seq(run, resolution) - max(0, min(context, MAX_CONTEXT)))

    ceiling = FULL_MAX_CHARS if decision.allows(Tier.FULL) else PASSAGE_MAX_CHARS
    limit = max(200, min(max_chars or DEFAULT_MAX_CHARS, ceiling))

    rows = list(
        run.paragraphs.filter(seq__gte=start)
        .order_by("seq")
        .only("id", "seq", "page", "section", "text", "locator_key")
        .prefetch_related("sentences")[:60]
    )
    chosen, used = [], 0
    for paragraph in rows:
        if chosen and used + len(paragraph.text) > limit:
            break
        chosen.append(paragraph)
        used += len(paragraph.text)

    # Reading order: a passage is never cut, and once one does not fit what is left of the budget, neither it nor
    # anything after it is returned (or charged); they are listed in `left_out`. The assistant's `Text.passage`
    # applies the same rule (#182).
    items = [disclosure.Item(chars=len(p.text), locator=p.locator_key, payload=p) for p in chosen]
    charged = disclosure.record(request, decision, items, in_order=True)
    kept = [item.payload for item in charged.kept]

    paragraphs = []
    for paragraph in kept:
        item = {
            "page": paragraph.page,
            "section": paragraph.section,
            "text": paragraph.text,
            "sentences": [
                {"start": sentence.start, "end": sentence.end, "page": sentence.page}
                for sentence in sorted(paragraph.sentences.all(), key=lambda s: s.seq)
            ],
        }
        if paragraph.locator_key:
            item["uri"] = uris.locator_uri(entry.pk, paragraph.locator_key)
            item["url"] = citation.cite_url(entry.pk, paragraph.locator_key)
        paragraphs.append(item)

    next_seq = None
    if kept:
        last = kept[-1].seq
        if charged.left_out:
            next_seq = charged.left_out[0].payload.seq
        elif run.paragraphs.filter(seq__gt=last).exists():
            next_seq = last + 1
    regions = []
    if kept:
        pages = (kept[0].page, kept[-1].page)
        for region in (
            run.regions.visible().filter(page__range=pages, locator_key__isnull=False).order_by("page", "seq")
        ):
            regions.append(
                {
                    "uri": uris.locator_uri(entry.pk, region.locator_key),
                    "kind": region.kind,
                    "page": region.page,
                    "label": citation.label_of(region.printed_caption or region.caption),
                }
            )
    payload = {
        "entry_id": str(entry.pk),
        "title": entry.title,
        "paragraphs": paragraphs,
        "regions": regions,
        "next_cursor": str(next_seq) if next_seq is not None else None,
        "access": _receipt(entry, decision, charged),
        # `resource_link` blocks: the regions on the pages read, attachable as resources.
        "resource_links": [
            {"uri": region["uri"], "name": region["label"] or f"{region['kind']} p. {region['page']}"}
            for region in regions
        ],
    }
    if charged.left_out:
        payload["left_out"] = [
            {"uri": uris.locator_uri(entry.pk, item.locator), "chars": item.chars}
            for item in charged.left_out
            if item.locator
        ]
    return payload


# -- regions ---------------------------------------------------------------------------------------------------


def region(request, value: str) -> dict:
    address = parse_address(value)
    if not address.key:
        raise Invalid("`uri` must address a figure, table or equation: evilflowers://entry/<id>/locator/<key>.")
    entry = visible_entry(request, address.entry_id)
    resolution = _resolve(entry, address.key)
    if resolution.locator.kind not in ("figure", "table", "equation"):
        raise Invalid(f"That locator is a {resolution.locator.kind}; use `read` for text.")
    decision = decide(request, entry)
    described = decide(request, entry, generated=True)
    payload = {
        "uri": value if value.startswith("evilflowers://") else citation.cite_url(entry.pk, address.key),
        "entry_id": str(entry.pk),
        "kind": resolution.locator.kind,
        "page": resolution.source_page,
        "resolution": resolution.resolution,
        "url": citation.cite_url(entry.pk, address.key),
        "access": citation.access_of(decision, entry.pk),
    }
    found = resolution.region
    if found is None:
        return payload
    payload["label"] = citation.label_of(found.printed_caption or found.caption)
    items = []
    if decision.allows(Tier.SNIPPET):
        if found.printed_caption:
            payload["caption"] = found.printed_caption
        if found.image_path:
            payload["crop_url"] = request.build_absolute_uri(
                reverse("files:entry-region-image", kwargs={"entry_id": entry.pk, "region_id": found.pk})
            )
    if described.allows(Tier.SNIPPET) and found.description:
        payload["description"] = found.description
    if decision.allows(Tier.PASSAGE):
        for name in ("markdown", "latex"):
            value_text = getattr(found, name)
            if value_text:
                items.append(disclosure.Item(chars=len(value_text), locator=address.key, payload=(name, value_text)))
    charged = disclosure.record(request, decision, items)
    for item in charged.kept:
        name, value_text = item.payload
        payload[name] = value_text
    payload["access"] = _receipt(entry, decision, charged)
    return payload


# -- search ----------------------------------------------------------------------------------------------------


def _scope(entry_ids: Sequence[str], catalog_id: Optional[str]) -> QueryDict:
    scope = QueryDict(mutable=True)
    if entry_ids:
        scope["id"] = ",".join(str(entry_id) for entry_id in entry_ids)
    if catalog_id:
        scope["catalog_id"] = str(catalog_id)
    return scope


def search_passages(request, query: str, *, limit: int, per_entry: int, scope=None, surface: str = Surface.MCP):
    """Passage search for every surface that hands passages to a model (#182): the in-app assistant
    (`search_library_text`, the evidence pack, study mode) and the MCP tools.

    `apps.extraction.discovery.search_passages` with what is shown logged by one rule (`disclosure.record`, as
    snippets: never charged to a budget). Each passage carries its `locator_key`. Returns the same result object.
    """
    from apps.extraction.discovery import search_passages as discover

    decisions: dict = {}
    found = discover(
        request, query, limit=limit, per_entry=per_entry, scope=scope, surface=surface, decisions_out=decisions
    )
    for item in found.items:
        decision = decisions.get(str(item.entry_id))
        shown = [
            disclosure.Item(chars=len(passage.text), locator=getattr(passage, "locator_key", None))
            for passage in item.passages
            if passage.text
        ]
        if decision is not None and shown:
            disclosure.record(request, decision, shown, charge=False)
    return found


def search(request, query: str, *, entry_ids=(), catalog_id=None, kinds=KINDS, limit: int = 8) -> dict:
    """Passages and regions about `query` as addresses with a snippet and score; Anthropic `search_result`-shaped
    (`source` is the `/cite` link, `title`, `content`)."""
    from apps.core.models import Entry
    from apps.extraction import locators, regions as region_search
    from apps.mcp import uris

    limit = max(1, min(limit, 20))
    scope = _scope(entry_ids, catalog_id)
    hits, degraded = [], False

    if "passage" in kinds:
        found = search_passages(request, query, limit=limit, per_entry=2, scope=scope, surface=Surface.MCP)
        degraded = found.degraded
        for item in found.items:
            for passage in item.passages:
                key = passage.locator_key
                if key is None:
                    continue
                hits.append(
                    {
                        "kind": "passage",
                        "entry_id": str(item.entry_id),
                        "key": key,
                        "page": passage.page,
                        "section": passage.section,
                        "score": passage.score,
                        "matched": passage.matched,
                        "text": passage.text,
                        # Logged by `search_passages` already.
                        "logged": True,
                    }
                )
    region_kinds = tuple(kind for kind in kinds if kind in ("figure", "table"))
    if region_kinds:
        found = region_search.search(request, query, kinds=region_kinds, limit=limit, scope=scope, surface=Surface.MCP)
        degraded = degraded or found.degraded
        keys = locators.keys_for(region_ids=[hit.region_id for hit in found.hits])
        for hit in found.hits:
            key = keys.get(str(hit.region_id))
            if key is None:
                continue
            hits.append(
                {
                    "kind": hit.kind,
                    "entry_id": str(hit.entry_id),
                    "key": key,
                    "page": hit.page,
                    "score": hit.score,
                    "matched": list(hit.matched),
                    "text": hit.caption,
                }
            )

    entries = {
        str(entry.pk): entry
        for entry in Entry.objects.filter(pk__in={hit["entry_id"] for hit in hits}).select_related("catalog")
    }
    decisions = access.decide_many(request, list(entries.values()), Surface.MCP, visible=True)
    hits.sort(key=lambda hit: -hit["score"])
    results, logged = [], {}
    for hit in hits[: limit * 2]:
        entry = entries.get(hit["entry_id"])
        if entry is None:
            continue
        decision = decisions[hit["entry_id"]]
        text = hit.pop("text")
        already_logged = hit.pop("logged", False)
        if not decision.allows(Tier.SNIPPET):
            text = None
        elif text:
            text = text[:SNIPPET_CHARS]
            if not already_logged:
                logged.setdefault(hit["entry_id"], []).append(disclosure.Item(chars=len(text), locator=hit["key"]))
        url = citation.cite_url(entry.pk, hit["key"])
        results.append(
            {
                **hit,
                "uri": uris.locator_uri(entry.pk, hit["key"]),
                "url": url,
                "title": entry.title,
                # search_result shape: `source` and `content`.
                "source": url,
                "content": text,
                "access": citation.access_of(decision, entry.pk),
            }
        )
    for entry_id, items in logged.items():
        disclosure.record(request, decisions[entry_id], items, charge=False)
    return {"query": query, "degraded": degraded, "items": results}


# -- packs -----------------------------------------------------------------------------------------------------


def pack(
    request, query: str, *, purpose: str = "", budget_chars: int = DEFAULT_PACK_CHARS, entry_ids=(), catalog_id=None
):
    """Labelled passages `[E1]`, `[E2]`… for `query` within `budget_chars`, stored under a hash id (`pack_id`)."""
    from apps.content.models import ContentPack
    from apps.core.models import Entry
    from apps.extraction.discovery import search_passages
    from apps.extraction.models import Paragraph
    from apps.mcp import uris

    budget_chars = max(500, min(budget_chars or DEFAULT_PACK_CHARS, MAX_PACK_CHARS))
    found = search_passages(
        request, query, limit=8, per_entry=3, scope=_scope(entry_ids, catalog_id), surface=Surface.MCP
    )
    ids = [passage.paragraph_id for item in found.items for passage in item.passages]
    paragraphs = {
        paragraph.pk: paragraph
        for paragraph in Paragraph.objects.filter(pk__in=ids).only("id", "page", "section", "text", "locator_key")
    }
    entries = {
        str(entry.pk): entry
        for entry in Entry.objects.filter(pk__in=[item.entry_id for item in found.items])
        .select_related("catalog", "language")
        .prefetch_related("entry_authors__author")
    }
    decisions = access.decide_many(request, list(entries.values()), Surface.MCP, visible=True)

    passages, stored, withheld, used = [], [], [], 0
    budget_left = {}
    for item in found.items:
        entry = entries.get(str(item.entry_id))
        if entry is None:
            continue
        decision = decisions[str(entry.pk)]
        for hit in item.passages:
            paragraph = paragraphs.get(hit.paragraph_id)
            if paragraph is None or not paragraph.locator_key:
                continue
            if not decision.allows(Tier.PASSAGE):
                withheld.append(
                    {
                        "uri": uris.locator_uri(entry.pk, paragraph.locator_key),
                        **citation.access_of(decision, entry.pk),
                    }
                )
                continue
            if len(passages) >= MAX_PACK_PASSAGES or used + len(paragraph.text) > budget_chars:
                continue
            charged = disclosure.record(
                request,
                decision,
                [disclosure.Item(chars=len(paragraph.text), locator=paragraph.locator_key)],
                budget_left=budget_left.get(str(entry.pk)),
            )
            if decision.metered:
                budget_left[str(entry.pk)] = charged.budget_left
            if charged.left_out:
                withheld.append(
                    {
                        "uri": uris.locator_uri(entry.pk, paragraph.locator_key),
                        **citation.access_of(decision, entry.pk),
                        "reason": "budget",
                    }
                )
                continue
            label = f"E{len(passages) + 1}"
            used += len(paragraph.text)
            passages.append(
                {
                    "label": label,
                    "uri": uris.locator_uri(entry.pk, paragraph.locator_key),
                    "url": citation.cite_url(entry.pk, paragraph.locator_key),
                    "entry_id": str(entry.pk),
                    "title": entry.title,
                    "page": paragraph.page,
                    "section": paragraph.section,
                    "text": paragraph.text,
                }
            )
            stored.append(
                {
                    "label": label,
                    "entry_id": str(entry.pk),
                    "locator_key": paragraph.locator_key,
                    "page": paragraph.page,
                }
            )

    user = getattr(request, "user", None)
    user_id = user.pk if user is not None and user.is_authenticated else None
    digest = hashlib.sha256(
        "\x00".join(
            [str(user_id), query, purpose] + [f"{item['entry_id']}/{item['locator_key']}" for item in stored]
        ).encode()
    ).hexdigest()[:20]
    ContentPack.objects.update_or_create(
        key=digest, defaults={"user_id": user_id, "query": query[:2000], "purpose": purpose[:2000], "items": stored}
    )
    return {
        "pack_id": digest,
        "query": query,
        "purpose": purpose,
        "budget_chars": budget_chars,
        "used_chars": used,
        "degraded": found.degraded,
        "passages": passages,
        "withheld": withheld,
    }


def pack_citation(request, pack_id: str, label: str) -> dict:
    """The citation of label `E#` of a pack this caller built; the gate is passed again."""
    from apps.content.models import ContentPack
    from apps.mcp import uris

    user = getattr(request, "user", None)
    user_id = user.pk if user is not None and user.is_authenticated else None
    found = ContentPack.objects.filter(key=pack_id, user_id=user_id).first()
    if found is None:
        raise NotFound("Content pack", pack_id)
    item = next((item for item in found.items if item["label"] == label.strip("[]")), None)
    if item is None:
        raise NotFound("Label", label)
    return citation_for(request, uris.locator_uri(item["entry_id"], item["locator_key"]))


__all__ = [
    "Invalid",
    "NotFound",
    "citation_for",
    "my_access",
    "outline",
    "pack",
    "pack_citation",
    "parse_address",
    "read",
    "region",
    "search",
    "visible_entry",
]

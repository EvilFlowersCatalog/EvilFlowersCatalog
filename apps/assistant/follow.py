"""`follow`: from a cited passage or figure to what it links to (IP-030 §4).

One read-only tool over the links already in Postgres, plus the AGE graph for `related`:

| Relation | From | Data |
|---|---|---|
| `next` / `previous` | `E#` | the paragraphs after / before it (`seq ± 1`) |
| `figure` | `E#` | the figures and tables it refers to (`extraction_reference`) → `F#` |
| `mentions` | `F#` | the paragraphs that refer to the figure |
| `similar_elsewhere` | `E#` | `similar` paragraph links into other publications the reader may see and read |
| `section` | `E#` | the paragraphs of its section in reading order, within the page budget |
| `related` | `E#` | sections of other publications sharing its section's concepts or similar to it (graph) |
| `look` | `F#` | what the figure shows: caption, the paragraph that refers to it, and with vision on, the model's view |
| `compare` | `E#` (+ `entry_id`) | the passage, and how another book explains the same thing over `SIMILAR_TO` (#157) |
| `prerequisites` | `E#` | sections defining a concept its section only uses (graph, as `section_prerequisites`): earlier sections and other books first, `later: true` last; the first paragraph of each (#186) |
| `cited_by` | `E#` / `F#` | publications whose reference lists cite its publication (#178), with the reference item's page |
| `cites` | `E#` / `F#` | publications of the library its publication's reference list cites |

Everything returned is labelled into the chat's ledger, so a followed passage is citable like any other. `cited_by`
and `cites` return publications (`id`, `title`), not passages: the model shows them with `displayBooks`.
"""

import json
import logging
from typing import List, Optional

from django.conf import settings

from apps.assistant import crossbook
from apps.assistant.library import Text, ToolContext, label_units
from apps.mcp.errors import ToolError

logger = logging.getLogger(__name__)

RELATIONS = (
    "next",
    "previous",
    "figure",
    "mentions",
    "similar_elsewhere",
    "section",
    "related",
    "prerequisites",
    "look",
    "compare",
    "cited_by",
    "cites",
)
#: Relations of a label's publication rather than of the passage or figure (#186).
CITATION_RELATIONS = ("cited_by", "cites")

#: Paragraphs returned for `next` / `previous`.
NEIGHBOURS = 2
#: Paragraphs of a section returned at most.
SECTION_PARAGRAPHS = 12
#: Results of `similar_elsewhere`, `mentions`, `related`.
LINKS = 3

SPECIFICATION = (
    "Follow a label to what it links to. F# only: mentions, look (what it shows). compare: how book entry_id "
    "explains an E#. prerequisites: what to read first. cited_by, cites: publications citing its book, or cited.",
    {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "relation": {"type": "string", "enum": list(RELATIONS)},
            "entry_id": {"type": "string"},
        },
        "required": ["label", "relation"],
    },
)


def follow(context: ToolContext, arguments: dict) -> dict:
    from apps.assistant.evidence import normalise

    label = normalise(str(arguments.get("label") or ""))
    relation = str(arguments.get("relation") or "").strip().lower()
    if relation not in RELATIONS:
        raise ToolError(f"`relation` must be one of: {', '.join(RELATIONS)}.")
    if label is None or context.ledger is None or label not in context.ledger.sources:
        raise ToolError("`label` must be an E# or F# label from a result in this chat.")
    source = context.ledger.sources[label]

    if relation in CITATION_RELATIONS:
        return _citing(context, label, source, relation)
    if label.startswith("F"):
        if relation not in ("mentions", "look"):
            raise ToolError("A figure (F#) can be followed by `mentions` or `look`.")
        return _from_figure(context, label, source, relation)
    if relation in ("mentions", "look"):
        raise ToolError("`mentions` and `look` follow a figure (F#).")
    paragraph_id = source.get("paragraph_id")
    if not paragraph_id or str(paragraph_id).startswith("page:"):
        raise ToolError("This label is a whole page, not a paragraph; search inside the publication instead.")
    if relation == "compare":
        from apps.assistant import crossbook

        return crossbook.compare(context, label, source, str(arguments.get("entry_id") or "").strip() or None)
    return _from_passage(context, label, source, relation)


def _paragraph(paragraph_id):
    from apps.extraction.models import Paragraph

    paragraph = Paragraph.objects.filter(pk=paragraph_id).select_related("run__acquisition__entry").first()
    if paragraph is None:
        raise ToolError("This passage is no longer in the library's index; search again.")
    return paragraph


def _text(context: ToolContext, entry) -> Text:
    return Text.of(context, entry)


def _result(context: ToolContext, label: str, relation: str, passages: List[dict], figures=(), note=None) -> dict:
    passages = [passage for passage in passages if passage]
    figures = [figure for figure in figures if figure]
    payload = {"from": label, "relation": relation, "passages": passages, "figures": list(figures)}
    language_note = crossbook.mark_languages(context, payload["passages"] + payload["figures"])
    issued = label_units(context.ledger, payload["passages"] + payload["figures"])
    if issued:
        payload["evidence"] = issued
    if note or not (passages or figures):
        payload["note"] = note or "Nothing to follow there."
    if language_note:
        payload["language_note"] = language_note
    return payload


def _from_passage(context: ToolContext, label: str, source: dict, relation: str) -> dict:
    from apps.extraction.models import Paragraph, ParagraphLink, Reference

    paragraph = _paragraph(source["paragraph_id"])
    entry = paragraph.run.acquisition.entry
    text = _text(context, entry)
    if text.run.pk != paragraph.run_id:
        raise ToolError("This passage is from an older processing of the publication; search again.")

    if relation in ("next", "previous"):
        if relation == "next":
            rows = Paragraph.objects.filter(run=paragraph.run, seq__gt=paragraph.seq).order_by("seq")[:NEIGHBOURS]
        else:
            rows = reversed(
                list(Paragraph.objects.filter(run=paragraph.run, seq__lt=paragraph.seq).order_by("-seq")[:NEIGHBOURS])
            )
        return _result(context, label, relation, [text.passage(row) for row in rows])

    if relation == "figure":
        regions = [
            reference.region
            for reference in Reference.objects.filter(paragraph=paragraph, region__isnull=False).select_related(
                "region"
            )
        ]
        unique = list({region.pk: region for region in regions}.values())
        return _result(context, label, relation, [], [text.figure(region) for region in unique])

    if relation == "section":
        return _section(context, label, text, paragraph)

    if relation == "similar_elsewhere":
        return _similar(context, label, paragraph)

    if relation == "prerequisites":
        return _prerequisites(context, label, text, paragraph)

    return _related(context, label, text, paragraph)


def _section(context: ToolContext, label: str, text: Text, paragraph) -> dict:
    from apps.extraction.models import Paragraph

    rows = list(
        Paragraph.objects.filter(run=paragraph.run, section=paragraph.section).order_by("seq")[
            : SECTION_PARAGRAPHS * 2
        ]
    )
    reading = context.reading
    entry_id = str(text.entry.pk)
    budget = reading.budget_for(entry_id) if reading is not None else settings.EVILFLOWERS_ASSISTANT_MAX_PAGES_READ
    pages = reading.pages_for(entry_id) if reading is not None else []
    passages, stopped = [], False
    for row in rows[:SECTION_PARAGRAPHS]:
        if row.page not in pages:
            if len(pages) >= budget:
                stopped = True
                break
            pages.append(row.page)
        passages.append(text.passage(row))
    note = None
    if stopped:
        note = "Reading budget reached; this is the start of the section."
    elif len(rows) > SECTION_PARAGRAPHS:
        note = "The section continues; follow the last passage with `next` for more."
    return _result(context, label, "section", passages, note=note)


def _similar(context: ToolContext, label: str, paragraph) -> dict:
    from apps.content import access
    from apps.core.models import Entry
    from apps.extraction.discovery import _visible
    from apps.extraction.models import ParagraphLink

    links = list(
        ParagraphLink.objects.filter(source=paragraph, kind=ParagraphLink.Kind.SIMILAR, target__run__is_active=True)
        .exclude(target__run__acquisition__entry_id=paragraph.run.acquisition.entry_id)
        .select_related("target__run__acquisition")
        .order_by("-score")[: LINKS * 4]
    )
    entry_ids = {link.target.run.acquisition.entry_id for link in links}
    visible = _visible(context.request, entry_ids)
    entries = {entry.pk: entry for entry in Entry.objects.filter(pk__in=visible)}
    readable = access.readable_ids(
        context.request, entries.values(), access.Tier.PASSAGE, access.Surface.ASSISTANT, visible=True
    )
    passages = []
    for link in links:
        entry = entries.get(link.target.run.acquisition.entry_id)
        if entry is None or str(entry.pk) not in readable:
            continue
        passage = Text(entry, link.target.run, lambda page: page).passage(link.target)
        passage["score"] = round(link.score or 0, 4)
        passages.append(passage)
        if len(passages) >= LINKS:
            break
    return _result(context, label, "similar_elsewhere", passages)


def _related(context: ToolContext, label: str, text: Text, paragraph) -> dict:
    from apps.content import access
    from apps.core.models import Entry
    from apps.extraction.models import Paragraph
    from apps.graph import projection, queries

    if not paragraph.section:
        raise ToolError("This passage has no section to relate.")
    if not projection.available():
        return _result(
            context, label, "related", [], note="Related sections are not available; use similar_elsewhere."
        )
    visible = queries.visible_entry_ids(context.request)
    found = queries.related_sections(projection.section_key(text.entry.pk, paragraph.section), visible, LINKS * 2)
    entries = {str(entry.pk): entry for entry in Entry.objects.filter(pk__in=[item["entry_id"] for item in found])}
    # `found` is already limited to the visible entries.
    readable = access.readable_ids(
        context.request, entries.values(), access.Tier.PASSAGE, access.Surface.ASSISTANT, visible=True
    )
    passages = []
    for item in found:
        entry = entries.get(item["entry_id"])
        if entry is None or item["entry_id"] not in readable:
            continue
        first = Paragraph.objects.filter(run_id=item["run_id"], section=item["title"]).order_by("seq").first()
        if first is None:
            continue
        passage = Text(entry, first.run, lambda page: page).passage(first)
        passage["via"] = item.get("via")
        passages.append(passage)
        if len(passages) >= LINKS:
            break
    return _result(context, label, "related", passages)


def _prerequisites(context: ToolContext, label: str, text: Text, paragraph) -> dict:
    """What to read before the passage's section (#186): the graph query behind `section_prerequisites`.

    Earlier sections of the same book and sections of other books first; a section further on in the same book
    last, marked `later` ("defined later in this book"). Each as its first paragraph, labelled like `related`.
    """
    from apps.content import access
    from apps.core.models import Entry
    from apps.extraction.models import Paragraph
    from apps.graph import projection, queries

    if not paragraph.section:
        raise ToolError("This passage has no section to find prerequisites for.")
    if not projection.available():
        return _result(context, label, "prerequisites", [], note="Prerequisites are not available; use related.")
    visible = queries.visible_entry_ids(context.request)
    found = queries.prerequisites(projection.section_key(text.entry.pk, paragraph.section), visible, LINKS * 2)
    own = str(text.entry.pk)
    others = Entry.objects.filter(pk__in=[item["entry_id"] for item in found if item["entry_id"] != own])
    entries = {str(entry.pk): entry for entry in others}
    readable = access.readable_ids(
        context.request, entries.values(), access.Tier.PASSAGE, access.Surface.ASSISTANT, visible=True
    )
    passages = []
    for item in found:
        if item["entry_id"] == own:
            # The same book: its pages as this chat numbers them (the reader's copy when bound).
            reader = text
            run_id = text.run.pk
        else:
            entry = entries.get(item["entry_id"])
            if entry is None or item["entry_id"] not in readable:
                continue
            reader, run_id = None, item["run_id"]
        first = Paragraph.objects.filter(run_id=run_id, section=item["title"]).order_by("seq").first()
        if first is None:
            continue
        if reader is None:
            reader = Text(entries[item["entry_id"]], first.run, lambda page: page)
        passage = reader.passage(first)
        if passage is None:
            continue
        if item.get("later"):
            passage["later"] = True
        passages.append(passage)
        if len(passages) >= LINKS:
            break
    return _result(context, label, "prerequisites", passages)


#: Publications `cited_by` / `cites` return at most.
CITING = 8


def _citing(context: ToolContext, label: str, source: dict, relation: str) -> dict:
    """Publications citing the label's publication, or cited by it (#178, #186), among those the reader may see.

    Publications, not passages: the catalogue record is enough to show them as cards, so a Readium LCP book is
    listed like any other. `cited_by` carries the page of the reference item that cites it.
    """
    from apps.extraction.bibliography import citations_of

    entry_id = source.get("entry_id")
    if not entry_id:
        raise ToolError("This label has no publication to follow.")
    found = citations_of(context.request, entry_id)
    publications, seen = [], set()
    for item in found.get(relation) or []:
        if item["entry_id"] in seen:
            continue
        seen.add(item["entry_id"])
        publication = {"id": item["entry_id"], "title": item.get("title")}
        if relation == "cited_by" and item.get("page"):
            publication["page"] = item["page"]
        publications.append(publication)
        if len(publications) >= CITING:
            break
    payload = {"from": label, "relation": relation, "passages": [], "figures": [], "publications": publications}
    if not publications:
        payload["note"] = (
            "No publication in this library cites it."
            if relation == "cited_by"
            else "Its reference list cites no publication of this library."
        )
    return payload


def _from_figure(context: ToolContext, label: str, source: dict, relation: str) -> dict:
    from apps.extraction.models import Reference, Region

    region = Region.objects.filter(pk=source["region_id"]).select_related("run__acquisition__entry").first()
    if region is None:
        raise ToolError("This figure is no longer in the library's index; search again.")
    text = _text(context, region.run.acquisition.entry)
    referring = [
        reference.paragraph
        for reference in Reference.objects.filter(region=region).select_related("paragraph").order_by("paragraph__seq")
    ]
    if relation == "mentions":
        unique = list({paragraph.pk: paragraph for paragraph in referring}.values())[:LINKS]
        return _result(context, label, relation, [text.passage(paragraph) for paragraph in unique])

    figure = text.figure(region)
    context_text = referring[0].text[:600] if referring else None
    figure["shows"] = describe(region, context_text, context.language, book=context.book) or region.description or None
    passages = [text.passage(referring[0])] if referring else []
    payload = _result(context, label, relation, passages, [figure])
    if not figure["shows"]:
        payload["note"] = "Only the caption and the passage that refers to it are known about this figure."
    return payload


def vision_enabled() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_ASSISTANT_VISION", False))


def describe(
    region, context_text: Optional[str], language: Optional[str] = None, client=None, book=None
) -> Optional[str]:
    """The model's description of a figure's crop (Q4: only on request, and only with vision on).

    Cached for a day per region. A separate request on its own slot: it never displaces a chat's cached state.
    """
    import base64

    from django.core.cache import cache

    from apps.assistant import prompts
    from apps.files.storage import get_storage

    if not vision_enabled() or not region.image_path:
        return None
    key = f"evilflowers:assistant:figure:{region.pk}"
    cached = cache.get(key)
    if cached:
        return cached
    storage = get_storage()
    try:
        with storage.open(region.image_path, "rb") as file:
            image = base64.b64encode(file.read()).decode("ascii")
    except Exception:
        logger.warning("Figure crop %s could not be read", region.pk)
        return None
    caption = region.printed_caption or region.caption or ""
    content = f"Printed caption: {caption or '(none)'}"
    if context_text:
        content += f"\n\nParagraph referring to it:\n{context_text}"
    name = prompts.language_name(language)
    if name:
        content += f"\n\nWrite in {name}.{prompts.not_czech(language)}"
    messages = [
        {"role": "system", "content": prompts.render("vision")},
        {"role": "user", "content": content, "images": [image]},
    ]
    if client is None:
        from apps.assistant.llm import client as make_client

        client = make_client()
    text = ""
    try:
        for delta in client.chat(messages, max_tokens=300):
            text += delta.content
            if book is not None:
                book(delta)
    except Exception:
        logger.exception("Vision request for figure %s failed", region.pk)
        return None
    text = " ".join(text.split())
    if text:
        cache.set(key, text, 86400)
    return text or None


def slim(payload: dict) -> dict:
    from apps.assistant.evidence import strip

    return strip(payload)


def as_text(payload: dict) -> str:
    return json.dumps(slim(payload), ensure_ascii=False, default=str)

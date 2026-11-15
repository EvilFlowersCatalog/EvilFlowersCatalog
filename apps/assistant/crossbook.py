"""Evidence from other books, and in other languages (#157).

The library's section vectors are multilingual (e5) and `SIMILAR_TO` (#151) joins sections of different books that
explain the same thing, whatever language they are written in. Three uses:

- **Widening.** A question asked inside one book (a bound chat, or a chat about one publication) is answered from
  that book first. When all the evidence comes from it, the pack adds up to `EVILFLOWERS_ASSISTANT_PACK_ELSEWHERE`
  passages reached over `SIMILAR_TO` from the sections cited, as `from_other_books`.
- **Compare.** `follow(E#, "compare", entry_id?)`: the passage's section in book A, followed over `SIMILAR_TO` into
  book B (or the most similar other book), so the model can answer side by side, citing both.
- **Language.** A passage in another language than the reader's carries its `language` and a `marker` in the
  reader's language ("(anglicky)", "(in German)"); the payload says once to answer in the reader's language, to put
  the marker after the citation and never to present a translation as a quotation.

Visibility: the graph is asked about the caller's visible entries only (`queries.visible_entry_ids`), and every
passage goes through `Text.of`, so a hidden, Readium LCP or IP-blocked publication never becomes evidence. Without
Apache AGE nothing is added and nothing fails.
"""

from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings

from apps.assistant.library import Text, ToolContext
from apps.mcp.errors import ToolError

#: Passages from other books the pack adds at most, when the setting is not given.
ELSEWHERE = 2

#: Passages of the other book `compare` returns.
COMPARE_PASSAGES = 2

#: Key of the widened evidence in the pack.
FROM_OTHER_BOOKS = "from_other_books"

#: The marker after a citation of a passage in another language, by the reader's language, then the passage's.
MARKERS: Dict[str, Dict[str, str]] = {
    "sk": {
        "en": "(anglicky)",
        "de": "(nemecky)",
        "cs": "(česky)",
        "fr": "(francúzsky)",
        "es": "(španielsky)",
        "it": "(taliansky)",
        "hu": "(maďarsky)",
        "pl": "(poľsky)",
        "ru": "(rusky)",
        "sk": "(slovensky)",
    },
    "en": {
        "en": "(in English)",
        "de": "(in German)",
        "cs": "(in Czech)",
        "fr": "(in French)",
        "es": "(in Spanish)",
        "it": "(in Italian)",
        "hu": "(in Hungarian)",
        "pl": "(in Polish)",
        "ru": "(in Russian)",
        "sk": "(in Slovak)",
    },
}


def elsewhere_limit() -> int:
    return max(int(getattr(settings, "EVILFLOWERS_ASSISTANT_PACK_ELSEWHERE", ELSEWHERE)), 0)


def _reader_language(language: Optional[str]) -> str:
    code = (language or "").split("-")[0].lower()
    return code if code in MARKERS else "sk"


def marker(reader_language: Optional[str], language: Optional[str]) -> Optional[str]:
    """`(anglicky)` for an English passage and a Slovak reader; None when the languages agree or one is unknown."""
    reader = _reader_language(reader_language)
    if not language or language == reader:
        return None
    return MARKERS[reader].get(language) or (f"({language})" if reader == "sk" else f"(in {language})")


def mark_languages(context: ToolContext, units: List[dict]) -> Optional[str]:
    """Mark the units in another language than the reader's, in place; the note for the model, or None."""
    from apps.core.models import Entry

    entry_ids = {str(unit["entry_id"]) for unit in units if unit and unit.get("entry_id")}
    if not entry_ids:
        return None
    languages = {
        str(pk): code
        for pk, code in Entry.objects.filter(pk__in=entry_ids, language__isnull=False).values_list(
            "pk", "language__alpha2"
        )
    }
    marked = []
    for unit in units:
        if not unit:
            continue
        language = languages.get(str(unit.get("entry_id")))
        sign = marker(context.language, language)
        if sign:
            unit["language"] = language
            unit["marker"] = sign
            marked.append(sign)
    if not marked:
        return None
    reader = _reader_language(context.language)
    name = {"sk": "Slovak", "en": "English"}[reader]
    return (
        f"Some evidence is in another language (its `marker`). Answer in {name}; after the citation of such a "
        f"passage add its marker, e.g. [E1] {marked[0]}. Never present your translation as a quotation."
    )


def _paragraph_rows(paragraph_ids: Iterable[str]) -> List[Tuple[str, str, str]]:
    """`(run_id, section, paragraph_id)` of these paragraphs that have a section, in the order given."""
    from apps.extraction.models import Paragraph

    ids = [str(pk) for pk in paragraph_ids if pk and not str(pk).startswith("page:")]
    rows = {
        str(pk): (str(run_id), section)
        for pk, run_id, section in Paragraph.objects.filter(pk__in=ids, section__isnull=False).values_list(
            "pk", "run_id", "section"
        )
    }
    return [(rows[pk][0], rows[pk][1], pk) for pk in ids if pk in rows]


def similar_sections(
    context: ToolContext, cited: List[Tuple[str, str]], exclude: Iterable[str], only: Optional[str] = None
) -> List[dict]:
    """Sections of other visible publications `SIMILAR_TO` the cited `(run_id, section)`s, best first.

    `exclude` are entry ids never returned (the book asked about); `only` keeps one publication. Empty without the
    graph.
    """
    from apps.graph import projection, queries

    if not cited or not projection.available():
        return []
    excluded = {str(entry_id) for entry_id in exclude}
    visible = [entry_id for entry_id in queries.visible_entry_ids(context.request) if entry_id not in excluded]
    if only is not None:
        visible = [entry_id for entry_id in visible if entry_id == str(only)]
    if not visible:
        return []
    best: Dict[str, dict] = {}
    for run_id, section in dict.fromkeys(cited):
        for item in queries.related_sections(projection.section_key(run_id, section), visible, limit=10):
            # SIMILAR_TO only: a shared concept alone is a topic, not the same explanation (#157).
            if "similar" not in (item.get("via") or "") or str(item["entry_id"]) in excluded:
                continue
            known = best.get(item["key"])
            if known is None or item["score"] > known["score"]:
                best[item["key"]] = item
    return sorted(best.values(), key=lambda item: (-item["score"], item["key"]))


def _closest(run_id, section: Optional[str], vector, count: int) -> list:
    """Paragraphs of a run (of one section, when given) closest to `vector`; in reading order without one."""
    from pgvector.django import CosineDistance

    from apps.extraction.models import Paragraph

    rows = Paragraph.objects.filter(run_id=run_id)
    if section is not None:
        rows = rows.filter(section=section)
    if vector is not None:
        rows = rows.filter(embedding__isnull=False).annotate(distance=CosineDistance("embedding", vector))
        found = list(rows.order_by("distance", "seq")[:count])
        if found or section is None:
            return found
        rows = Paragraph.objects.filter(run_id=run_id, section=section)
    elif section is None:
        return []
    return list(rows.order_by("seq")[:count])


def _passages_of(context: ToolContext, items: List[dict], vectors: Dict[str, object], limit: int, per_section=1):
    """Readable passages of these graph sections: per section the paragraph(s) closest to what was cited."""
    from apps.core.models import Entry

    entries = {str(entry.pk): entry for entry in Entry.objects.filter(pk__in=[item["entry_id"] for item in items])}
    passages: List[dict] = []
    for item in items:
        entry = entries.get(str(item["entry_id"]))
        if entry is None:
            continue
        try:
            text = Text.of(context, entry)
        except ToolError:
            continue
        if str(text.run.pk) != str(item["run_id"]):
            # The graph still holds an older run of this publication.
            continue
        for paragraph in _closest(text.run.pk, item["title"], vectors.get(item["key"]), per_section):
            passage = text.passage(paragraph)
            if passage:
                passage["via"] = "similar"
                passages.append(passage)
        if len(passages) >= limit:
            break
    return passages[:limit]


def widen(context: ToolContext, passages: List[dict]) -> List[dict]:
    """Passages from other books for a pack whose passages all come from one book (#157); [] otherwise."""
    from apps.extraction.models import Paragraph

    limit = elsewhere_limit()
    entry_ids = {passage["entry_id"] for passage in passages}
    if not limit or len(entry_ids) != 1:
        return []
    rows = _paragraph_rows(passage["paragraph_id"] for passage in passages)
    found = similar_sections(context, [(run_id, section) for run_id, section, _pk in rows], exclude=entry_ids)
    if not found:
        return []
    # Each section of another book is read where it is closest to the best cited paragraph.
    vector = Paragraph.objects.filter(pk=rows[0][2]).values_list("embedding", flat=True).first()
    return _passages_of(context, found, {item["key"]: vector for item in found}, limit)


def compare(context: ToolContext, label: str, source: dict, other_id: Optional[str]) -> dict:
    """`follow(label, "compare")`: how book A (the label's) and book B explain the same thing, side by side."""
    from apps.assistant.follow import _paragraph, _result
    from apps.assistant.library import entry_for

    paragraph = _paragraph(source["paragraph_id"])
    entry = paragraph.run.acquisition.entry
    text = Text.of(context, entry)
    if text.run.pk != paragraph.run_id:
        raise ToolError("This passage is from an older processing of the publication; search again.")
    other = None
    if other_id:
        other = entry_for(context, other_id)
        if other.pk == entry.pk:
            raise ToolError("`compare` needs another publication's `entry_id`, not this one's.")

    found = []
    if paragraph.section:
        found = similar_sections(
            context, [(str(paragraph.run_id), paragraph.section)], exclude=[str(entry.pk)], only=other and other.pk
        )
    if other is None and found:
        # The most similar other book: its best section.
        found = [item for item in found if item["entry_id"] == found[0]["entry_id"]]
    theirs = _passages_of(
        context, found[:1], {item["key"]: paragraph.embedding for item in found[:1]}, COMPARE_PASSAGES, 2
    )
    if not theirs and other is not None:
        # No SIMILAR_TO into that book (or no graph): its paragraphs closest to this one.
        other_text = Text.of(context, other)
        theirs = [
            passage
            for passage in (
                other_text.passage(row) for row in _closest(other_text.run.pk, None, paragraph.embedding, 2)
            )
            if passage
        ]
        for passage in theirs:
            passage["via"] = "nearest"

    ours = text.passage(paragraph)
    if not theirs:
        where = f"'{other.title}'" if other is not None else "another publication"
        return _result(
            context,
            label,
            "compare",
            [ours],
            note=f"No section of {where} explains the same thing. Search it with search_library_text and its "
            "entry_id, or answer from this passage alone.",
        )
    payload = _result(context, label, "compare", [ours] + theirs)
    a_labels = [ours["label"]] if ours and ours.get("label") else [label]
    b_labels = [passage["label"] for passage in theirs if passage.get("label")]
    b_title = theirs[0].get("title")
    payload["compare"] = {
        "a": {"entry_id": str(entry.pk), "title": entry.title, "labels": a_labels},
        "b": {"entry_id": theirs[0]["entry_id"], "title": b_title, "labels": b_labels},
    }
    instruction = (
        f"Answer side by side: how '{entry.title}' explains it [{', '.join(a_labels)}], then how '{b_title}' "
        f"explains it [{', '.join(b_labels)}]; then say what differs. Cite each side."
    )
    payload["note"] = f"{payload['note']} {instruction}" if payload.get("note") else instruction
    return payload

"""Read-only REST over the library graph and the vectors behind it, for the portal (#152, IP-030 §4).

| Route | Backed by |
|---|---|
| `GET /api/v1/entries/map` | publication vectors, mutual nearest neighbours (`similarity.entry_neighbours`) |
| `GET /api/v1/entries/{id}/chapters` | the outline and intra-book `similar` paragraph links per pair of sections |
| `GET /api/v1/sections/{key}/related` | `queries.related_sections` |
| `GET /api/v1/sections/{key}/prerequisites` | `queries.prerequisites` |
| `GET /api/v1/concepts?q=`, `GET /api/v1/concepts/{key}` | `Concept` and its `MENTIONS` |
| `GET /api/v1/graph/overview` | `queries.top_concepts` and counts |

Publications similar to one are `GET /api/v1/entries/{id}/similar` (IP-018), unchanged.

Rules: every answer is limited to publications the caller may see (`EntryFilter`), and a section or concept
reached only through hidden publications is "not found", never a neighbour, path or count. Without Apache AGE
the graph routes answer 200 with empty lists. The map and chapters are deterministic and carry an ETag of the
active runs they are made of. Each route records a `graph.query` span.
"""

import hashlib
from collections import Counter
from http import HTTPStatus
from typing import Dict, List, Optional
from uuid import UUID

from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.filters.entries import EntryFilter
from apps.api.response import SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import DetailType, ProblemDetailException
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.extraction import similarity
from apps.extraction.discovery import _entry, _entry_run
from apps.extraction.models import ExtractionRun
from apps.extraction.outline import outline
from apps.graph import age, projection, queries
from evil_flowers_catalog import tracing

#: Parameters of the map itself; the rest scope it like `GET /api/v1/entries` (e.g. `catalog_id`).
MAP_PARAMS = {"k"}

#: Intra-book `similar` paragraph links before two sections of one book are joined on the chapter map.
MIN_CHAPTER_LINKS = 2

#: Results of the list routes by default, and at most.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

#: Seconds a computed map is kept; its key is its ETag, so a change of the active runs is a new key.
MAP_CACHE_SECONDS = 24 * 3600


class GraphSectionSerializer(Serializer):
    #: Section key (`/api/v1/sections/{key}/...`).
    key: str
    entry_id: UUID
    #: The heading; None where the reader may not read the publication's text here (Readium LCP, outside its
    #: IP ranges): a heading is the publication's text, as in the assistant's outline.
    title: Optional[str] = None
    #: Page of the original PDF where the section starts, 1-based: open the reader there.
    first_page: Optional[int] = None
    score: float


class RelatedSectionSerializer(GraphSectionSerializer):
    #: `concepts`, `similar` or `concepts+similar`.
    via: str
    #: #190: how the decider relates this section to the one asked about, from its reader's side: `same`,
    #: `prerequisite` (read this one first), `next` (builds on the one asked about), `example` (an example of it),
    #: `general` (the idea the one asked about is an example of) or `loose`. Null until the pair is typed.
    relation: Optional[str] = None
    #: The decider's probability of `relation`.
    relation_p: Optional[float] = None


class PrerequisiteSerializer(GraphSectionSerializer):
    #: True for a section further on in the same book: "defined later in this book", not "read first".
    later: bool


class ConceptSummarySerializer(Serializer):
    key: str
    term: str
    #: UDC main class of the publications the concept is used in ("6"), None when they are filed nowhere.
    field: Optional[str] = None
    publications: int
    sections: int = 0


class ConceptSectionSerializer(Serializer):
    key: str
    entry_id: UUID
    #: None where the reader may not read the publication's text here (see `GraphSectionSerializer`).
    title: Optional[str] = None
    first_page: Optional[int] = None
    #: `defines` or `discusses`.
    role: str


class SubjectSerializer(Serializer):
    id: UUID
    term: str


class ConceptSerializer(Serializer):
    key: str
    term: str
    field: Optional[str] = None
    sections: List[ConceptSectionSerializer]
    entries: List[UUID]
    subjects: List[SubjectSerializer]


class TopConceptSerializer(Serializer):
    key: str
    term: str
    field: Optional[str] = None
    subjects: int
    publications: int


class OverviewSerializer(Serializer):
    #: False when the graph is not installed: every list is empty.
    available: bool
    publications: int
    sections: int
    concepts: int
    similar: int
    top_concepts: List[TopConceptSerializer]


class MapNodeSerializer(Serializer):
    id: UUID
    catalog_id: UUID
    title: str
    #: Category terms (UDC codes in the STU catalogs).
    udc: List[str]
    pages: Optional[int] = None


class MapEdgeSerializer(Serializer):
    source: UUID
    target: UUID
    #: Cosine of the two publication vectors after the mean is subtracted.
    score: float


class LibraryMapSerializer(Serializer):
    nodes: List[MapNodeSerializer]
    edges: List[MapEdgeSerializer]


class ChapterSerializer(Serializer):
    key: str
    seq: int
    #: None where the reader may not read the publication's text here (Readium LCP, outside its IP ranges).
    title: Optional[str] = None
    first_page: int
    last_page: int
    paragraphs: int
    #: False for front and back matter (contents, index, references ...).
    topical: bool


class ChapterLinkSerializer(Serializer):
    source: str
    target: str
    #: `similar` paragraph links between the two sections.
    links: int


class ChapterMapSerializer(Serializer):
    entry_id: UUID
    sections: List[ChapterSerializer]
    links: List[ChapterLinkSerializer]


def _limit(request) -> int:
    try:
        return max(1, min(int(request.GET.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    except ValueError:
        return DEFAULT_LIMIT


def _not_found(message: str):
    return ProblemDetailException(message, status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND)


def _span(route: str):
    return tracing.span("graph.query", {"evilflowers.graph.route": route, "evilflowers.graph.age": age.available()})


def _rows(current, count: int) -> None:
    current.set_attribute("evilflowers.graph.rows", count)


def _etag_response(request, etag: str, payload) -> HttpResponse:
    quoted = f'"{etag}"'
    if request.headers.get("If-None-Match") == quoted:
        response = HttpResponse(status=HTTPStatus.NOT_MODIFIED)
    else:
        response = SingleResponse(request, data=payload)
    response["ETag"] = quoted
    # Per caller: what is in the map depends on what they may see.
    response["Cache-Control"] = "private, no-cache"
    response["Vary"] = "Authorization, Cookie"
    return response


def _readable(request, entry_ids) -> set:
    """Ids (as str) of these entries whose headings the reader may see here: the content gate's `outline`
    (under `parity`: not Readium LCP, inside the IP ranges). Callers pass entries already limited to the visible."""
    from apps.content import access

    ids = {str(entry_id) for entry_id in entry_ids}
    entries = Entry.objects.filter(pk__in=ids)
    return access.readable_ids(request, entries, access.Tier.OUTLINE, access.Surface.GRAPH, visible=True)


def _headings(request, items: List[dict]) -> List[dict]:
    """`items` with the `title` of every section the reader may not read here set to None.

    A heading is the publication's text, the rule the assistant's outline follows (IP-030): the key, the entry and
    the page stay, so the portal can still open the reader, which applies its own checks.
    """
    readable = _readable(request, [item["entry_id"] for item in items])
    return [item if str(item["entry_id"]) in readable else {**item, "title": None} for item in items]


def _visible_section(request, key: str) -> List[str]:
    """The caller's visible entry ids, after checking the section belongs to one of them (else 404)."""
    entries = queries.visible_entry_ids(request)
    entry_id = queries.section_entry(key)
    if entry_id is None or entry_id not in set(entries):
        raise _not_found(_("Section not found"))
    return entries


class SectionRelated(SecuredView):
    @openapi.metadata(
        description=(
            "Sections of other publications that use the same concepts or discuss the same thing "
            "(`SIMILAR_TO`, #151), best first. `via` says which; `relation` and `relation_p` say how the decision "
            "model relates the pair (#190), null until it typed it. Only publications the caller may see; 404 for "
            "a section of a publication they may not. A `title` is null for publications lent through Readium LCP "
            "or outside their allowed IP ranges. Empty without the graph."
        ),
        tags=["Graph"],
        summary="List related sections",
    )
    @openapi.response(HTTPStatus.OK, "Related sections", RelatedSectionSerializer)
    def get(self, request, key: str):
        with _span("section.related") as current:
            if not age.available():
                return SingleResponse(request, data=[])
            entries = _visible_section(request, key)
            found = _headings(request, queries.related_sections(key, entries, _limit(request)))
            _rows(current, len(found))
        return SingleResponse(
            request,
            data=[RelatedSectionSerializer(**item).model_dump(mode="json") for item in found],
        )


class SectionPrerequisites(SecuredView):
    @openapi.metadata(
        description=(
            "Sections that define a concept this one only discusses: what to read before it. Earlier sections "
            "of the same book and other books first; a section further on in the same book comes last with "
            "`later: true` (#153). Only publications the caller may see; 404 for a hidden section. A `title` is "
            "null for publications lent through Readium LCP or outside their allowed IP ranges."
        ),
        tags=["Graph"],
        summary="List a section's prerequisites",
    )
    @openapi.response(HTTPStatus.OK, "Prerequisites", PrerequisiteSerializer)
    def get(self, request, key: str):
        with _span("section.prerequisites") as current:
            if not age.available():
                return SingleResponse(request, data=[])
            entries = _visible_section(request, key)
            found = _headings(request, queries.prerequisites(key, entries, _limit(request)))
            _rows(current, len(found))
        return SingleResponse(request, data=[PrerequisiteSerializer(**item).model_dump(mode="json") for item in found])


class ConceptList(SecuredView):
    @openapi.metadata(
        description=(
            "Concepts the caller's publications define or discuss (from section summaries, IP-030), most used "
            "first. `q` matches the term, accents and case ignored. A term used in two fields (UDC main classes) "
            "is two concepts, each with its `field` (#153)."
        ),
        tags=["Graph"],
        summary="List concepts",
    )
    @openapi.response(HTTPStatus.OK, "Concepts", ConceptSummarySerializer)
    def get(self, request):
        with _span("concept.list") as current:
            found = queries.search_concepts(
                (request.GET.get("q") or "").strip()[:120], queries.visible_entry_ids(request), _limit(request)
            )
            _rows(current, len(found))
        return SingleResponse(
            request, data=[ConceptSummarySerializer(**item).model_dump(mode="json") for item in found]
        )


class ConceptDetail(SecuredView):
    @openapi.metadata(
        description=(
            "A concept with the caller's sections that define or discuss it (defining first), their publications "
            "and the subjects those are filed under. 404 when no publication the caller may see uses it. A "
            "section's `title` is null for publications lent through Readium LCP or outside their IP ranges."
        ),
        tags=["Graph"],
        summary="Get a concept",
    )
    @openapi.response(HTTPStatus.OK, "Concept", ConceptSerializer)
    def get(self, request, key: str):
        from apps.core.models import Category

        with _span("concept.detail") as current:
            found = queries.concept(key, queries.visible_entry_ids(request))
            if found is None:
                raise _not_found(_("Concept not found"))
            _rows(current, len(found["sections"]))
        entry_ids = list(dict.fromkeys(str(section["entry_id"]) for section in found["sections"]))
        subjects = Category.objects.filter(entries__in=entry_ids).distinct().order_by("term").values("id", "term")
        payload = ConceptSerializer(
            key=found["key"],
            term=found["term"],
            field=found["field"],
            sections=[ConceptSectionSerializer(**section) for section in _headings(request, found["sections"])],
            entries=entry_ids,
            subjects=[SubjectSerializer(**subject) for subject in subjects],
        )
        return SingleResponse(request, data=payload.model_dump(mode="json"))


class GraphOverview(SecuredView):
    @openapi.metadata(
        description=(
            "What the graph holds of the caller's publications: how many publications, sections, concepts and "
            "cross-book `SIMILAR_TO` edges, and the concepts that span the most subjects. Counts never include "
            "publications the caller may not see. `available` is false (and everything empty) without the graph."
        ),
        tags=["Graph"],
        summary="Get the graph overview",
    )
    @openapi.response(HTTPStatus.OK, "Overview", OverviewSerializer)
    def get(self, request):
        with _span("graph.overview"):
            available = age.available()
            entries = queries.visible_entry_ids(request) if available else []
            counts = queries.overview_counts(entries)
            top = queries.top_concepts(entries, _limit(request)) if available else []
        payload = OverviewSerializer(
            available=available, top_concepts=[TopConceptSerializer(**item) for item in top], **counts
        )
        return SingleResponse(request, data=payload.model_dump(mode="json"))


def library_map(request, scope) -> tuple:
    """`(etag, payload)` of the map of the caller's processed publications (#152)."""
    visible = EntryFilter(scope, queryset=Entry.objects.all(), request=request).qs.values("pk")
    runs = {}
    changed = {}
    for run_id, finished_at, entry_id, entry_updated_at, provenance in (
        ExtractionRun.objects.filter(is_active=True, acquisition__entry__in=visible)
        .order_by("acquisition__entry_id", "-finished_at", "pk")
        .values_list("pk", "finished_at", "acquisition__entry_id", "acquisition__entry__updated_at", "provenance")
    ):
        # An entry with several processed acquisitions: its latest run.
        model = ((provenance or {}).get("paragraphs") or {}).get("embedding_model") or ""
        runs.setdefault(entry_id, (run_id, finished_at, model))
        changed[entry_id] = entry_updated_at
    # The active runs, the entries' own changes and their categories (an M2M change does not touch
    # `updated_at`) make the map: its ETag and cache key.
    filed = sorted(
        f"{entry_id}:{category_id}"
        for entry_id, category_id in Entry.categories.through.objects.filter(entry_id__in=list(runs)).values_list(
            "entry_id", "category_id"
        )
    )
    fingerprint = (
        "|".join(
            f"{entry_id}:{run_id}:{finished_at.isoformat() if finished_at else ''}:"
            f"{changed[entry_id].isoformat() if changed[entry_id] else ''}"
            for entry_id, (run_id, finished_at, _model) in sorted(runs.items(), key=lambda item: str(item[0]))
        )
        + "#"
        + ",".join(filed)
    )
    etag = hashlib.sha1(
        f"map:v2:{similarity.ENTRY_NEIGHBOURS}:{similarity.MIN_LANGUAGE_BOOKS}:{fingerprint}".encode()
    ).hexdigest()
    cached = cache.get(f"graph:map:{etag}")
    if cached is not None:
        return etag, cached

    entries = (
        Entry.objects.filter(pk__in=list(runs))
        .prefetch_related("categories")
        .only("pk", "title", "catalog_id")
        .order_by("pk")
    )
    pages = dict(
        ExtractionRun.objects.filter(pk__in=[run[0] for run in runs.values()]).values_list("pk", "page_count")
    )
    nodes = [
        MapNodeSerializer(
            id=entry.pk,
            catalog_id=entry.catalog_id,
            title=entry.title,
            udc=sorted(category.term for category in entry.categories.all()),
            pages=pages.get(runs[entry.pk][0]),
        ).model_dump(mode="json")
        for entry in entries
    ]
    # One embedding model: the one most of the map's publications were processed with.
    models = Counter(model for _run, _at, model in runs.values())
    model = models.most_common(1)[0][0] if models else ""
    vectors, languages = {}, {}
    for entry_id, language_id, vector in ExtractionRun.objects.filter(
        pk__in=[run_id for run_id, _at, run_model in runs.values() if run_model == model],
        embedding__isnull=False,
    ).values_list("acquisition__entry_id", "acquisition__entry__language_id", "embedding"):
        vectors[entry_id] = vector.to_numpy()
        languages[entry_id] = language_id
    edges = [
        MapEdgeSerializer(source=a, target=b, score=score).model_dump(mode="json")
        # Centred per language: German books otherwise cluster by language as much as by topic (#151).
        for a, b, score in similarity.entry_neighbours(vectors, languages=languages)
    ]
    payload = LibraryMapSerializer(nodes=nodes, edges=edges).model_dump(mode="json")
    cache.set(f"graph:map:{etag}", payload, MAP_CACHE_SECONDS)
    return etag, payload


def entry_neighbours(request, scope) -> Dict[str, List[tuple]]:
    """`{entry_id: [(neighbour_id, score), ...]}`, best first: the edges of the caller's library map (#160, #161).

    The same mutual kNN as `GET /api/v1/entries/map`, over the publications the caller may see within `scope`
    (e.g. `{"catalog_id": ...}`), so a hidden publication is never a neighbour and never changes a visible pair.
    Only entries with an active extraction run take part. Cached with the map (its ETag is the key).
    """
    _etag, payload = library_map(request, scope)
    found: Dict[str, List[tuple]] = {}
    for edge in payload["edges"]:
        found.setdefault(edge["source"], []).append((edge["target"], edge["score"]))
        found.setdefault(edge["target"], []).append((edge["source"], edge["score"]))
    for items in found.values():
        items.sort(key=lambda item: (-item[1], item[0]))
    return found


class LibraryMap(SecuredView):
    @openapi.metadata(
        description=(
            "A map of the caller's processed publications (#152): nodes with title, category terms (UDC) and page "
            "count; edges between publications that are among each other's 4 nearest by their vector after the "
            "mean is subtracted (mutual kNN, #151). Computed over the publications the caller may see only, so a "
            "hidden publication never shapes an edge. Other parameters of `GET /api/v1/entries` (e.g. "
            "`catalog_id`) narrow it. Deterministic, with an ETag: send `If-None-Match` for a 304."
        ),
        tags=["Entries"],
        summary="Get the library map",
    )
    @openapi.response(HTTPStatus.OK, "Library map", LibraryMapSerializer)
    def get(self, request):
        scope = request.GET.copy()
        for name in MAP_PARAMS:
            scope.pop(name, None)
        with _span("entries.map") as current:
            etag, payload = library_map(request, scope)
            _rows(current, len(payload["edges"]))
        return _etag_response(request, etag, payload)


def chapter_map(entry_id, readable: bool = True) -> tuple:
    """`(etag, payload)`: an entry's sections in reading order and how strongly its sections relate.

    `readable` False hides the headings (Readium LCP, outside the IP ranges); the structure stays.
    """
    run = _entry_run(entry_id)
    if run is None:
        return "none", ChapterMapSerializer(entry_id=entry_id, sections=[], links=[]).model_dump(mode="json")
    finished = run.finished_at.isoformat() if run.finished_at else ""
    etag = hashlib.sha1(f"chapters:v2:{MIN_CHAPTER_LINKS}:{run.pk}:{finished}:{readable}".encode()).hexdigest()
    sections = outline(run)
    keys = projection.section_keys(entry_id, [section.title for section in sections])
    order = {section.title: index for index, section in enumerate(sections)}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.section, t.section, COUNT(*) FROM extraction_paragraph_link l "
            "JOIN extraction_paragraph s ON s.id = l.source_id "
            "JOIN extraction_paragraph t ON t.id = l.target_id "
            "WHERE l.kind = 'similar' AND s.run_id = %s AND t.run_id = %s "
            "AND s.section IS NOT NULL AND t.section IS NOT NULL AND s.section <> t.section "
            "GROUP BY s.section, t.section",
            [run.pk, run.pk],
        )
        rows = cursor.fetchall()
    # One edge per pair of sections, in reading order, whichever way the paragraphs were linked.
    pairs: Dict[tuple, int] = {}
    for source, target, count in rows:
        if source not in order or target not in order:
            continue
        pair = tuple(sorted((source, target), key=order.get))
        pairs[pair] = pairs.get(pair, 0) + count
    links = [
        ChapterLinkSerializer(source=keys[a], target=keys[b], links=count)
        for (a, b), count in sorted(pairs.items(), key=lambda item: (order[item[0][0]], order[item[0][1]]))
        if count >= MIN_CHAPTER_LINKS
    ]
    payload = ChapterMapSerializer(
        entry_id=entry_id,
        sections=[
            ChapterSerializer(
                key=keys[section.title],
                seq=index,
                title=section.title if readable else None,
                first_page=section.first_page,
                last_page=section.last_page,
                paragraphs=section.paragraphs,
                topical=similarity.is_topical(section.title),
            )
            for index, section in enumerate(sections)
        ],
        links=links,
    )
    return etag, payload.model_dump(mode="json")


class EntryChapters(SecuredView):
    @openapi.metadata(
        description=(
            "An entry's sections in reading order (`key` for `/api/v1/sections/{key}/...`, pages of the original "
            "PDF) and, between pairs of its sections, how many paragraphs text-service found similar (at least "
            "2): a map of how the book's chapters relate (#152). Empty for an entry not processed yet. Headings "
            "are null for entries lent through Readium LCP and outside the entry's allowed IP ranges. "
            "Deterministic, with an ETag: send `If-None-Match` for a 304."
        ),
        tags=["Entries"],
        summary="Get an entry's chapter map",
    )
    @openapi.response(HTTPStatus.OK, "Chapter map", ChapterMapSerializer)
    def get(self, request, entry_id: UUID):
        from apps.content import access

        entry = _entry(request, entry_id)
        readable = access.decide(request, entry, surface=access.Surface.GRAPH, visible=True).allows(
            access.Tier.OUTLINE
        )
        with _span("entries.chapters") as current:
            etag, payload = chapter_map(entry_id, readable=readable)
            _rows(current, len(payload["links"]))
        return _etag_response(request, etag, payload)

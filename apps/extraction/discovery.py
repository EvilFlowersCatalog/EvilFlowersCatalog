"""Recommendations and entry figures from the extraction index (IP-018 Phase 8).

- Publications similar to an entry, by the publication vector of its active run
  (the mean of its paragraph embeddings).
- Publications similar to a passage a reader selected, by its paragraphs.
- The figures of an entry for its detail page (Q18: open-access entries only;
  an entry lent through Readium LCP shows its figures to borrowers in the viewer).

Every result is limited to entries the caller may see, with `EntryFilter`'s
catalog access control, and never includes the entry asked about.
"""

import logging
import mimetypes
from http import HTTPStatus
from typing import Dict, List, Optional
from uuid import UUID

from django import forms
from django.conf import settings
from django.http import FileResponse
from django_api_forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _
from pgvector.django import CosineDistance

from apps import openapi
from apps.api.filters.entries import EntryFilter
from apps.api.response import SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import DetailType, ProblemDetailException, ValidationException
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.extraction.models import ExtractionRun, Paragraph, Region
from apps.extraction.reader import EmbeddingUnavailable, ReaderExtraction
from apps.extraction.views import ReaderView
from apps.files.storage import get_storage
from apps.files.views import _check_ip_block

logger = logging.getLogger(__name__)

#: Recommendations returned by default, and at most.
DEFAULT_LIMIT = 8
MAX_LIMIT = 24

#: Paragraphs ranked for a passage before they are grouped per publication.
PASSAGE_PARAGRAPHS = 200

#: Characters of a selection embedded for "similar to this passage".
MAX_PASSAGE_CHARS = 2000

#: Passage search: paragraphs shown per publication by default, and at most.
DEFAULT_PER_ENTRY = 1
MAX_PER_ENTRY = 3
#: Characters of a paragraph returned as a passage's text.
PASSAGE_TEXT_CHARS = 600
#: Query parameters of passage search itself; the rest scope it like `GET /api/v1/entries`.
PASSAGE_PARAMS = {"query", "limit", "per_entry", "page", "order_by", "include"}


class SimilarPassageForm(Form):
    text = forms.CharField(required=False, max_length=20000)


class SimilarEntrySerializer(Serializer):
    entry_id: UUID
    #: Cosine similarity, 0-1.
    score: float


class PassageSerializer(Serializer):
    paragraph_id: UUID
    #: Page of the original PDF, 1-based (as the viewer shows an open-access entry).
    page: int
    section: Optional[str]
    #: Cosine similarity, 0-1, when the passage matched by meaning; else its word-match rank, 0-1.
    score: float
    #: The paragraph, cut at 600 characters; None when the reader may not read it here
    #: (licensed through Readium LCP, or outside the entry's allowed IP ranges).
    text: Optional[str]
    truncated: bool = False
    #: HTML-escaped excerpt with `<mark>` around the query's words, the same convention as
    #: `search.highlights` on entries (#113); None where `text` is None.
    snippet: Optional[str] = None
    #: How the passage matched: `semantic` (by meaning), `words` (the query's words, accents ignored), or both.
    matched: List[str] = []


class PassageHitSerializer(Serializer):
    entry_id: UUID
    catalog_id: Optional[UUID] = None
    #: The best passage's score.
    score: float
    passages: List[PassageSerializer]


class PassageSearchSerializer(Serializer):
    query: str
    #: True when the query could not be embedded (tei-embed unconfigured or down): only word matches.
    degraded: bool
    #: Publications the caller may see whose paragraphs are embedded by the current model.
    indexed: int
    #: Publications the caller may see with a processed PDF: searchable by words (#113).
    searchable: int = 0
    items: List[PassageHitSerializer]


class EntryRegionSerializer(Serializer):
    id: UUID
    kind: str
    #: Page of the PDF, as the viewer shows an open-access entry.
    page: int
    caption: Optional[str]
    printed_caption: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str]
    image_url: Optional[str]


def _limit(request) -> int:
    try:
        return max(1, min(int(request.GET.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
    except ValueError:
        return DEFAULT_LIMIT


def _visible(request, entry_ids) -> set:
    """The ids of these entries the caller may see."""
    queryset = Entry.objects.filter(pk__in=list(entry_ids))
    return set(EntryFilter({}, queryset=queryset, request=request).qs.values_list("pk", flat=True))


def _entry(request, entry_id: UUID) -> Entry:
    if entry_id not in _visible(request, [entry_id]):
        raise ProblemDetailException(
            _("Entry not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
        )
    return Entry.objects.select_related("catalog").get(pk=entry_id)


def _entry_run(entry_id) -> Optional[ExtractionRun]:
    return (
        ExtractionRun.objects.filter(acquisition__entry_id=entry_id, is_active=True).order_by("-finished_at").first()
    )


def _ranked(request, scores: Dict, limit: int) -> List[dict]:
    """`{entry_id: score}` as the caller's visible entries, best first."""
    visible = _visible(request, scores)
    ranked = sorted(
        ((entry_id, score) for entry_id, score in scores.items() if entry_id in visible), key=lambda x: -x[1]
    )
    return [
        SimilarEntrySerializer(entry_id=entry_id, score=round(score, 4)).model_dump(mode="json")
        for entry_id, score in ranked[:limit]
    ]


def _similar_to_run(request, entry_id, run: Optional[ExtractionRun], limit: int) -> List[dict]:
    if run is None or run.embedding is None:
        return []
    scores: Dict = {}
    neighbours = (
        ExtractionRun.objects.filter(is_active=True, embedding__isnull=False)
        .exclude(acquisition__entry_id=entry_id)
        .annotate(distance=CosineDistance("embedding", run.embedding))
        .order_by("distance")
        .values_list("acquisition__entry_id", "distance")[: limit * 5]
    )
    for other_entry_id, distance in neighbours:
        scores.setdefault(other_entry_id, 1 - distance)
    return _ranked(request, scores, limit)


def _similar_to_entry(request, entry_id, limit: int) -> Optional[List[dict]]:
    """By the entry vector (IP-021: title, authors, categories, summary), among entries of the same model.

    None when the entry has no vector yet, so the caller can fall back to the extraction run.
    """
    row = Entry.objects.filter(pk=entry_id).values("embedding", "embedding_model").first()
    if not row or row["embedding"] is None:
        return None
    neighbours = (
        Entry.objects.filter(embedding__isnull=False, embedding_model=row["embedding_model"])
        .exclude(pk=entry_id)
        .annotate(distance=CosineDistance("embedding", row["embedding"]))
        .order_by("distance")
        .values_list("pk", "distance")[: limit * 5]
    )
    return _ranked(request, {other_id: 1 - distance for other_id, distance in neighbours}, limit)


def similar_entries(request, entry_id, limit: int = DEFAULT_LIMIT) -> List[dict]:
    """Entries similar to one the caller may already see: by the entry vector, else its PDF.

    Shared by the REST view and the assistant's `find_similar` tool. The caller checks that
    `entry_id` is visible first (`_entry`).
    """
    similar = _similar_to_entry(request, entry_id, limit)
    if similar is None:
        similar = _similar_to_run(request, entry_id, _entry_run(entry_id), limit)
    return similar


class EntrySimilar(ReaderView):
    @openapi.metadata(
        description=(
            "Publications similar to an entry: by the entry vector (title, authors, categories and summary "
            "embedded with multilingual-e5, IP-021) when it exists, else by the mean of the paragraph "
            "embeddings of its processed PDF (IP-018). Only entries the caller may see, never the entry "
            "itself, best first. Resolve them with `GET /api/v1/entries?id=...`."
        ),
        tags=["Entries"],
        summary="List similar entries",
    )
    @openapi.response(HTTPStatus.OK, "Similar entries", SimilarEntrySerializer)
    def get(self, request, entry_id: UUID):
        _entry(request, entry_id)
        return SingleResponse(request, data=similar_entries(request, entry_id, _limit(request)))


class UserAcquisitionSimilar(ReaderView):
    @openapi.metadata(
        description=(
            "Publications that discuss what a passage of the reader's copy says: the passage is embedded "
            "and ranked against every processed publication's paragraphs, grouped per publication by its "
            "best paragraph (IP-018). Only entries the caller may see, never the copy's own entry."
        ),
        tags=["Entries"],
        summary="List entries similar to a passage",
    )
    @openapi.response(HTTPStatus.OK, "Similar entries", SimilarEntrySerializer)
    def post(self, request, user_acquisition_id: UUID):
        user_acquisition = self._user_acquisition(request, user_acquisition_id)
        entry_id = user_acquisition.acquisition.entry_id
        form = SimilarPassageForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)
        text = (form.cleaned_data.get("text") or "").strip()[:MAX_PASSAGE_CHARS]
        limit = _limit(request)

        run = _entry_run(entry_id)
        if not text:
            # No passage: the publication as a whole.
            return SingleResponse(request, data=_similar_to_run(request, entry_id, run, limit))

        extraction = ReaderExtraction(user_acquisition.acquisition_id, [], run=run) if run else None
        if extraction is None or not extraction.embedding_model:
            return SingleResponse(request, data=[])
        try:
            vector = extraction.embed(text, "similar")
        except EmbeddingUnavailable:
            logger.warning("Similar passages: text-service did not embed in time")
            return SingleResponse(request, data=[])

        scores: Dict = {}
        paragraphs = (
            _embedded_paragraphs(extraction.embedding_model)
            .exclude(run__acquisition__entry_id=entry_id)
            .annotate(distance=CosineDistance("embedding", vector))
            .order_by("distance")
            .values_list("run__acquisition__entry_id", "distance")[:PASSAGE_PARAGRAPHS]
        )
        for other_entry_id, distance in paragraphs:
            scores.setdefault(other_entry_id, 1 - distance)
        return SingleResponse(request, data=_ranked(request, scores, limit))


def _embedded_paragraphs(model: str):
    """Paragraphs of active runs embedded by `model`: vectors of two models are never compared."""
    return Paragraph.objects.filter(
        run__is_active=True,
        embedding__isnull=False,
        run__provenance__paragraphs__embedding_model=model,
    )


def _int_param(request, name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(request.GET.get(name) or default), maximum))
    except ValueError:
        return default


def _may_read_text(request, entry: Entry) -> bool:
    """Whether a passage's text may be shown outside the reader (IP-018 Q18/Q19, IP block)."""
    if entry.read_config("readium_enabled"):
        return False
    try:
        _check_ip_block(request, entry)
    except ProblemDetailException:
        return False
    return True


class EntryPassages(SecuredView):
    @openapi.metadata(
        description=(
            "Search inside publications (#113). Two rankings over the paragraphs of every processed PDF the "
            "caller may see (IP-018), fused by reciprocal rank: by meaning (the `query` embedded with "
            'multilingual-e5, IP-021) and by words (accents ignored; `"a phrase"`, `-excluded` and `or` as in '
            "entry search). Passages are grouped per publication by its best paragraph, best first; each "
            "says how it `matched` (`semantic`, `words`) and carries a `snippet`: HTML-escaped text with "
            "`<mark>` around the query's words, as `search.highlights` on entries. Other parameters of "
            "`GET /api/v1/entries` (e.g. `catalog_id`, `category_id`, `language`) narrow the publications "
            "searched. `per_entry` (1-3) passages per publication, `limit` publications (at most 24). A "
            "passage's `text` and `snippet` are null for entries lent through Readium LCP and outside the "
            "entry's allowed IP ranges; its `page` and score are still given. `degraded` is true when the "
            "query could not be embedded (word matches only); `indexed` counts the publications searchable "
            "by meaning, `searchable` those searchable by words. Resolve entries with "
            "`GET /api/v1/entries?id=...`."
        ),
        tags=["Entries"],
        summary="Search inside publications",
    )
    @openapi.response(HTTPStatus.OK, "Passages per publication", PassageSearchSerializer)
    def get(self, request):
        text = (request.GET.get("query") or "").strip()[:MAX_PASSAGE_CHARS]
        if not text:
            raise ProblemDetailException(
                _("Parameter query is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        # The caller's other parameters scope the search like `GET /api/v1/entries`.
        scope = request.GET.copy()
        for name in PASSAGE_PARAMS:
            scope.pop(name, None)
        result = search_passages(
            request,
            text,
            limit=_limit(request),
            per_entry=_int_param(request, "per_entry", DEFAULT_PER_ENTRY, MAX_PER_ENTRY),
            scope=scope,
        )
        return SingleResponse(request, data=result.model_dump(mode="json"))


def search_passages(
    request, text: str, *, limit: int = DEFAULT_LIMIT, per_entry: int = DEFAULT_PER_ENTRY, scope=None
) -> PassageSearchSerializer:
    """Paragraphs closest to `text` across every processed PDF the caller may see.

    Shared by the REST view and the assistant's `search_passages` tool. `scope` holds
    `EntryFilter` parameters (catalog, category, language, ...); access control is applied
    before ranking, as in entry search (IP-021). A passage's text is None where the reader
    may not read it outside the viewer (`_may_read_text`: Readium LCP, IP block).
    """
    from apps.core.search import embeddings
    from apps.core.search.query import rrf

    text = (text or "").strip()[:MAX_PASSAGE_CHARS]
    limit = max(1, min(limit, MAX_LIMIT))
    per_entry = max(1, min(per_entry, MAX_PER_ENTRY))
    visible = EntryFilter(scope if scope is not None else {}, queryset=Entry.objects.all(), request=request).qs.values(
        "pk"
    )
    # Every paragraph of an active run is searchable by words; by meaning only those embedded by the current model.
    active = Paragraph.objects.filter(run__is_active=True, run__acquisition__entry__in=visible)
    searchable = active.values("run__acquisition__entry_id").distinct().count()

    vector = embeddings.try_embed_query(text, "passages") if text else None
    model = None
    if vector is not None:
        try:
            model = embeddings.model_id()
        except embeddings.EmbeddingError:
            vector = None

    by_meaning: Dict = {}
    indexed = 0
    if vector is not None:
        paragraphs = _embedded_paragraphs(model).filter(run__acquisition__entry__in=visible)
        indexed = paragraphs.values("run__acquisition__entry_id").distinct().count()
        by_meaning = dict(
            paragraphs.annotate(distance=CosineDistance("embedding", vector))
            .filter(distance__lte=1 - settings.EVILFLOWERS_SEARCH_PASSAGE_MIN_SCORE)
            .order_by("distance")
            .values_list("pk", "distance")[:PASSAGE_PARAGRAPHS]
        )
    by_words = _word_matches(active, text) if text else {}

    # Reciprocal rank fusion (IP-021's k): a passage both channels rank high comes first.
    fused = [pk for pk, _score in rrf([list(by_meaning), list(by_words)], k=RRF_K)][:PASSAGE_PARAGRAPHS]
    rows = {
        row["pk"]: row
        for row in Paragraph.objects.filter(pk__in=fused).values(
            "pk", "page", "section", "text", "run__acquisition__entry_id", "run__acquisition__entry__catalog_id"
        )
    }

    hits: Dict = {}
    catalogs: Dict = {}
    for pk in fused:
        row = rows.get(pk)
        if row is None:
            continue
        entry_id = row["run__acquisition__entry_id"]
        catalogs[entry_id] = row["run__acquisition__entry__catalog_id"]
        passages = hits.setdefault(entry_id, [])
        if len(passages) < per_entry:
            passages.append(row)
        if len(hits) >= limit and all(len(p) >= per_entry for p in hits.values()):
            break
    entry_ids = list(hits)[:limit]
    readable = {entry.pk: _may_read_text(request, entry) for entry in Entry.objects.filter(pk__in=entry_ids)}
    snippets = _snippets(
        text, [row["pk"] for entry_id in entry_ids if readable.get(entry_id) for row in hits[entry_id]]
    )

    items = []
    for entry_id in entry_ids:
        passages = []
        for row in hits[entry_id]:
            may_read = bool(readable.get(entry_id))
            distance = by_meaning.get(row["pk"])
            matched = (["semantic"] if distance is not None else []) + (["words"] if row["pk"] in by_words else [])
            score = 1 - distance if distance is not None else by_words.get(row["pk"], 0.0)
            passages.append(
                PassageSerializer(
                    paragraph_id=row["pk"],
                    page=row["page"],
                    section=row["section"],
                    score=round(score, 4),
                    text=row["text"][:PASSAGE_TEXT_CHARS] if may_read else None,
                    truncated=may_read and len(row["text"]) > PASSAGE_TEXT_CHARS,
                    snippet=snippets.get(row["pk"]) if may_read else None,
                    matched=matched,
                )
            )
        items.append(
            PassageHitSerializer(
                entry_id=entry_id, catalog_id=catalogs.get(entry_id), score=passages[0].score, passages=passages
            )
        )
    return PassageSearchSerializer(
        query=text, degraded=vector is None, indexed=indexed, searchable=searchable, items=items
    )


#: Fusion constant of the two passage rankings (the usual 60, as in entry search).
RRF_K = 60

#: `ts_headline` options of a passage snippet: up to two fragments around the query's words.
SNIPPET_OPTIONS = 'MaxFragments=2, MinWords=8, MaxWords=30, FragmentDelimiter=" … ", StartSel=<mark>, StopSel=</mark>'


def _document(column: str) -> str:
    """The indexed text search document of a paragraph (migration 0005: accents folded, no stemming)."""
    from apps.core.search.ddl import SIMPLE_CONFIG

    return f"to_tsvector('{SIMPLE_CONFIG}'::regconfig, {column})"


def _tsquery() -> str:
    from apps.core.search.ddl import SIMPLE_CONFIG

    # Quotes for a phrase, `-word` to exclude, `or`: the same syntax as entry search.
    return f"websearch_to_tsquery('{SIMPLE_CONFIG}'::regconfig, %s)"


def _word_matches(paragraphs, text: str) -> Dict:
    """`{paragraph id: rank 0-1}` of paragraphs containing the query's words, best first (#113)."""
    from django.db.models import FloatField
    from django.db.models.expressions import RawSQL

    document = _document(f'"{Paragraph._meta.db_table}"."text"')
    return dict(
        paragraphs.extra(where=[f"{document} @@ {_tsquery()}"], params=[text])
        .annotate(rank=RawSQL(f"ts_rank_cd({document}, {_tsquery()}, 32)", [text], output_field=FloatField()))
        .order_by("-rank")
        .values_list("pk", "rank")[:PASSAGE_PARAGRAPHS]
    )


def _snippets(text: str, paragraph_ids: List) -> Dict:
    """HTML-escaped excerpts of these paragraphs with `<mark>` around the query's words."""
    from django.db import connection

    from apps.core.search.ddl import SIMPLE_CONFIG

    if not paragraph_ids or not text:
        return {}
    escaped = "replace(replace(replace(text, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')"
    sql = (
        f"SELECT id, ts_headline('{SIMPLE_CONFIG}'::regconfig, {escaped}, {_tsquery()}, '{SNIPPET_OPTIONS}') "
        f"FROM {Paragraph._meta.db_table} WHERE id = ANY(%s)"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [text, list(paragraph_ids)])
        return dict(cursor.fetchall())


class EntryFigures(ReaderView):
    """The figures of an entry's open-access PDF, for its detail page (Q18)."""

    def _open_run(self, request, entry_id: UUID) -> Optional[ExtractionRun]:
        entry = _entry(request, entry_id)
        _check_ip_block(request, entry)
        if entry.read_config("readium_enabled"):
            # Licensed: borrowers see figures in the viewer, nobody in the detail.
            return None
        return _entry_run(entry_id)

    @openapi.metadata(
        description=(
            "Figures (and with `kind`, tables or equations) of an entry's processed open-access PDF, in "
            "reading order, with the printed caption, the model's description and the crop. Empty for "
            "entries lent through Readium LCP and for entries not processed yet (IP-018 Q18)."
        ),
        tags=["Entries"],
        summary="List an entry's figures",
    )
    @openapi.response(HTTPStatus.OK, "Figures", EntryRegionSerializer)
    def get(self, request, entry_id: UUID):
        run = self._open_run(request, entry_id)
        if run is None:
            return SingleResponse(request, data=[])
        kind = request.GET.get("kind") or Region.Kind.FIGURE
        if kind not in Region.Kind.values:
            raise ProblemDetailException(_("Unknown region kind"), status=HTTPStatus.BAD_REQUEST)

        items = []
        for region in run.regions.filter(kind=kind).order_by("page", "seq"):
            image_url = None
            if region.image_path:
                image_url = request.build_absolute_uri(
                    reverse("files:entry-region-image", kwargs={"entry_id": entry_id, "region_id": region.pk})
                )
            items.append(
                EntryRegionSerializer(
                    id=region.pk,
                    kind=region.kind,
                    page=region.page,
                    caption=region.caption,
                    printed_caption=region.printed_caption,
                    description=region.description,
                    category=region.category,
                    image_url=image_url,
                ).model_dump(mode="json")
            )
        return SingleResponse(request, data=items)


class EntryRegionImage(EntryFigures):
    @openapi.metadata(description="The crop of a figure of an open-access entry, as an image.", tags=["Files"])
    def get(self, request, entry_id: UUID, region_id: UUID):
        run = self._open_run(request, entry_id)
        region = run and run.regions.filter(pk=region_id).first()
        storage = get_storage()
        if region is None or not region.image_path or not storage.exists(region.image_path):
            raise ProblemDetailException(_("Region image not found"), status=HTTPStatus.NOT_FOUND)
        response = FileResponse(
            storage.open(region.image_path, "rb"),
            content_type=mimetypes.guess_type(region.image_path)[0] or "image/webp",
        )
        response["Cache-Control"] = "private, max-age=3600"
        return response

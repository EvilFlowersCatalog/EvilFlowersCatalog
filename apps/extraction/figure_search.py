"""Figure and table search across the library (#159): `GET /api/v1/search?q=...&kind=figure|table`.

Returns regions, not entries. Ranking is `regions.search`: the region vector (e5, IP-021) and its search document
by words (GIN, migration 0010), fused by reciprocal rank, as passage search does for paragraphs. Access control
comes first: only publications the caller may see, inside their allowed IP ranges, and of those lent through
Readium LCP only the ones the caller holds a live loan of. The crop is the stored region image, served by
`files:entry-region-image` with the same checks (403 for an LCP title without a loan). `downloadable` says whether
a client may offer the crop for download: open access, with printing and sharing allowed for the entry.

Shared by the REST view and the MCP tool `search_figures`.
"""

from http import HTTPStatus
from typing import List, Optional, Sequence
from uuid import UUID

from django.urls import reverse
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import DetailType, ProblemDetailException
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.extraction import regions
from apps.extraction.discovery import region_downloadable
from apps.extraction.models import Region
from evil_flowers_catalog import tracing

#: Region kinds that can be searched: equations have no search document (`regions.document_text`).
KINDS = (Region.Kind.FIGURE.value, Region.Kind.TABLE.value)
#: #205: what `kind` also accepts: the decision model's label of the region (`table` is both).
DECISION_KINDS = regions.DECISION_KINDS
ALL_KINDS = tuple(dict.fromkeys(KINDS + DECISION_KINDS))

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_QUERY_CHARS = 500

#: Parameters of the figure search itself; the rest scope it like `GET /api/v1/entries` (e.g. `catalog_id`).
SEARCH_PARAMS = {"q", "query", "kind", "limit", "page", "order_by", "include"}


class FigureSearchHitSerializer(Serializer):
    entry_id: UUID
    #: The publication's title.
    title: str
    #: Page of the original PDF, 1-based.
    page: int
    region_id: UUID
    #: `figure` or `table`.
    kind: str
    #: The printed caption, else the model's description.
    caption: Optional[str] = None
    #: The model's description of the figure, when there is one.
    description: Optional[str] = None
    #: Cosine similarity, 0-1, when the region matched by meaning; else its word-match rank, 0-1.
    score: float
    #: How the region matched: `semantic`, `words`, or both.
    matched: List[str] = []
    #: The crop of the region (an image), with the same access check as the reader's; None without a crop.
    crop_url: Optional[str] = None
    #: Whether a client may offer the crop for download (open access, printing and sharing allowed).
    downloadable: bool = False
    #: #205: the decision model's label of the region (`chart`, `diagram`, `photo`, `map`, `table`, `formula`, ...)
    #: and its probability; null when undecided. Uncalibrated until the thresholds exist (knowledge#65).
    decision: Optional[str] = None
    decision_p: Optional[float] = None


class FigureSearchSerializer(Serializer):
    query: str
    #: The `kind` values searched: region kinds and decision labels.
    kinds: List[str]
    #: True when the query could not be embedded (tei-embed unconfigured or down): only word matches.
    degraded: bool
    items: List[FigureSearchHitSerializer]


def parse_kinds(value) -> List[str]:
    """`figure`, `table`, `chart,diagram` (or a list) to the `kind` values; `figure,table` when empty."""
    if not value:
        return list(KINDS)
    tokens = value if isinstance(value, (list, tuple)) else str(value).replace("|", ",").split(",")
    kinds = [token.strip() for token in tokens if token and token.strip()]
    unknown = [kind for kind in kinds if kind not in ALL_KINDS]
    if unknown:
        raise ProblemDetailException(
            _("Unknown region kind: %(kinds)s. Allowed: %(allowed)s.")
            % {"kinds": ", ".join(unknown), "allowed": ", ".join(ALL_KINDS)},
            status=HTTPStatus.BAD_REQUEST,
            detail_type=DetailType.VALIDATION_ERROR,
        )
    return list(dict.fromkeys(kinds)) or list(KINDS)


def split_kinds(kinds: Sequence[str]) -> tuple:
    """`(region kinds, decision labels)` of the `kind` values (#205).

    Only region kinds (`figure`, `table`): those regions, as before. Any decision label (`chart`, ...): regions with
    that label, plus every table when `table` is asked for; `figure` beside a label adds nothing, as a labelled
    region is a figure.
    """
    labels = [kind for kind in kinds if kind in DECISION_KINDS and kind != Region.Kind.TABLE]
    if not labels:
        return [kind for kind in kinds if kind in KINDS] or list(KINDS), None
    if Region.Kind.TABLE in kinds:
        labels.append(Region.Kind.TABLE.value)
    return list(KINDS), labels


def figure_search(
    request, text: str, *, kinds: Sequence[str] = KINDS, limit: int = DEFAULT_LIMIT, scope=None
) -> FigureSearchSerializer:
    text = (text or "").strip()[:MAX_QUERY_CHARS]
    limit = max(1, min(limit, MAX_LIMIT))
    region_kinds, labels = split_kinds(kinds)
    with tracing.span("search.figures", {"evilflowers.search.kinds": ",".join(kinds)}) as current:
        found = regions.search(
            request,
            text,
            kinds=tuple(region_kinds),
            limit=limit,
            scope=scope,
            licensed=True,
            labels=labels,
        )
        current.set_attribute("evilflowers.search.hits", len(found.hits))
        current.set_attribute("evilflowers.search.degraded", found.degraded)
    entries = {
        str(entry.pk): entry
        for entry in Entry.objects.filter(pk__in={hit.entry_id for hit in found.hits}).only("pk", "title", "config")
    }
    items = []
    for hit in found.hits:
        entry = entries.get(hit.entry_id)
        if entry is None:
            continue
        crop_url = None
        if hit.has_image:
            crop_url = request.build_absolute_uri(
                reverse("files:entry-region-image", kwargs={"entry_id": hit.entry_id, "region_id": hit.region_id})
            )
        items.append(
            FigureSearchHitSerializer(
                entry_id=hit.entry_id,
                title=entry.title,
                page=hit.page,
                region_id=hit.region_id,
                kind=hit.kind,
                caption=hit.caption,
                description=hit.description,
                score=hit.score,
                matched=list(hit.matched),
                crop_url=crop_url,
                downloadable=bool(crop_url) and region_downloadable(entry),
                decision=hit.decision,
                decision_p=hit.decision_p,
            )
        )
    return FigureSearchSerializer(query=text, kinds=list(kinds), degraded=found.degraded, items=items)


class FigureSearch(SecuredView):
    @openapi.metadata(
        description=(
            "Find figures and tables across the library (#159): returns regions, not entries. `q` is matched by "
            "meaning (the region's caption, referring paragraph and description embedded with multilingual-e5) "
            "and by words (accents ignored), fused by reciprocal rank. `kind` is `figure`, `table` or both "
            "(`figure,table`, the default); equations are not searchable. `kind` also takes the decision model's "
            "label of a region, `chart`, `diagram`, `photo`, `map`, `table` or `formula` (comma-separated, #205): "
            "only regions with that label (and every table for `table`). Each hit carries `decision` and "
            "`decision_p`, null when undecided; labels are uncalibrated until thresholds are set. Where a kind has "
            "thresholds, regions in review sort after kept ones. Only publications the caller may see, "
            "inside their allowed IP ranges; of those lent through Readium LCP only the ones the caller has a live "
            "loan of. Each hit has a `crop_url` (the region as an image, same access check) and `downloadable`, "
            "whether a client may offer it for download (open access, printing and sharing allowed). Other "
            "parameters of `GET /api/v1/entries` (e.g. `catalog_id`, `category_id`) narrow the publications "
            "searched. `limit` hits, at most 50."
        ),
        tags=["Entries"],
        summary="Search figures and tables",
    )
    @openapi.response(HTTPStatus.OK, "Figures and tables", FigureSearchSerializer)
    def get(self, request):
        text = (request.GET.get("q") or request.GET.get("query") or "").strip()
        if not text:
            raise ProblemDetailException(
                _("Parameter q is required"), status=HTTPStatus.BAD_REQUEST, detail_type=DetailType.VALIDATION_ERROR
            )
        kinds = parse_kinds(request.GET.get("kind"))
        try:
            limit = int(request.GET.get("limit") or DEFAULT_LIMIT)
        except ValueError:
            limit = DEFAULT_LIMIT
        scope = request.GET.copy()
        for name in SEARCH_PARAMS:
            scope.pop(name, None)
        result = figure_search(request, text, kinds=kinds, limit=limit, scope=scope)
        return SingleResponse(request, data=result.model_dump(mode="json"))

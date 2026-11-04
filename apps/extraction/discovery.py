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


class SimilarPassageForm(Form):
    text = forms.CharField(required=False, max_length=20000)


class SimilarEntrySerializer(Serializer):
    entry_id: UUID
    #: Cosine similarity, 0-1.
    score: float


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


class EntrySimilar(ReaderView):
    @openapi.metadata(
        description=(
            "Publications similar to an entry, by the mean of their paragraph embeddings (IP-018). Empty "
            "when the entry's PDF has not been processed. Only entries the caller may see, never the entry "
            "itself, best first. Resolve them with `GET /api/v1/entries?id=...`."
        ),
        tags=["Entries"],
        summary="List similar entries",
    )
    @openapi.response(HTTPStatus.OK, "Similar entries", SimilarEntrySerializer)
    def get(self, request, entry_id: UUID):
        _entry(request, entry_id)
        return SingleResponse(request, data=_similar_to_run(request, entry_id, _entry_run(entry_id), _limit(request)))


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
            vector = extraction.embed(text)
        except EmbeddingUnavailable:
            logger.warning("Similar passages: text-service did not embed in time")
            return SingleResponse(request, data=[])

        scores: Dict = {}
        paragraphs = (
            Paragraph.objects.filter(
                run__is_active=True,
                embedding__isnull=False,
                run__provenance__paragraphs__embedding_model=extraction.embedding_model,
            )
            .exclude(run__acquisition__entry_id=entry_id)
            .annotate(distance=CosineDistance("embedding", vector))
            .order_by("distance")
            .values_list("run__acquisition__entry_id", "distance")[:PASSAGE_PARAGRAPHS]
        )
        for other_entry_id, distance in paragraphs:
            scores.setdefault(other_entry_id, 1 - distance)
        return SingleResponse(request, data=_ranked(request, scores, limit))


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

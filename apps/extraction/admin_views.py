"""Extraction as the catalog's managers see it (IP-018).

The admin's "AI processing" tab of a publication: every run of every PDF of the
entry with its jobs, what the active run holds (counts, regions, paragraphs),
how deep the service queues are, and a way to queue the entry for (re)processing
without `manage.py backfill_extraction`.

Everything here requires `check_entry_manage` on the entry, so unlike the reader
endpoints it shows regions of any run (a failed or running one too) and never
narrows pages to a reader's range.
"""

import logging
import mimetypes
from datetime import datetime
from http import HTTPStatus
from typing import Dict, List, Optional
from uuid import UUID

from django import forms
from django.conf import settings
from django.db.models import BooleanField, Count, ExpressionWrapper, Q, Sum
from django.db.models.functions import Length
from django.http import FileResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django_api_forms import Form
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import DetailType, ProblemDetailException, ValidationException
from apps.core.models import Acquisition, Entry
from apps.core.views import SecuredView
from apps.extraction import services
from apps.extraction.models import ExtractionRun, ParagraphLink, Region, Sentence
from apps.files.storage import get_storage
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)

#: Celery queues of the extraction services, in pipeline order. Their depth is read
#: from the broker; a queue another broker serves simply reports nothing.
SERVICE_QUEUES = (
    ("analyzer", "EVILFLOWERS_EXTRACTION_QUEUE", "evilflowers_analyzer"),
    ("image", None, "evilflowers_image"),
    ("equation", None, "evilflowers_equation"),
    ("text", None, "evilflowers_text_worker"),
    ("embed", "EVILFLOWERS_EXTRACTION_EMBED_QUEUE", "evilflowers_text_embed"),
)

#: Messages of a queue scanned for a run's task, to tell its position.
QUEUE_SCAN_LIMIT = 5000

PARAGRAPHS_DEFAULT_LIMIT = 25
PARAGRAPHS_MAX_LIMIT = 100


class QueueForm(Form):
    #: One acquisition of the entry; every extractable PDF of the entry without it.
    acquisition_id = forms.UUIDField(required=False)
    #: Re-extract even when a run of the current pipeline version is done.
    force = forms.BooleanField(required=False)


class RunStatsSerializer(Serializer):
    figures: int = 0
    tables: int = 0
    equations: int = 0
    #: Regions with a stored crop.
    with_image: int = 0
    #: Regions with the caption printed in the document.
    with_printed_caption: int = 0
    #: Regions a multimodal model described.
    with_description: int = 0
    #: Distinct pages that hold at least one region.
    pages_with_regions: int = 0
    paragraphs: int = 0
    #: Paragraphs that carry an embedding (searchable, recommendable).
    paragraphs_embedded: int = 0
    sentences: int = 0
    #: Distinct outline sections the paragraphs fall under.
    sections: int = 0
    links_next: int = 0
    links_similar: int = 0
    #: Characters of paragraph text.
    characters: int = 0
    #: The publication vector exists (recommendations, "similar publications").
    has_embedding: bool = False


class RunSerializer(Serializer):
    id: UUID
    pipeline_version: int
    status: str
    is_active: bool
    page_count: Optional[int]
    jobs: Dict[str, str]
    provenance: dict
    error: Optional[str]
    storage_dir: str
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    #: Seconds from the analyzer starting the run to its last job reporting.
    duration: Optional[float] = None
    stats: Optional[RunStatsSerializer] = None


class QueueTaskSerializer(Serializer):
    #: The analyzer task the catalog sent for this acquisition and pipeline version.
    task_id: str
    queue: str
    #: True while the task waits in the broker (no worker took it yet).
    waiting: Optional[bool] = None
    #: 1 = next to be taken. None when not waiting or unknown.
    position: Optional[int] = None


class AcquisitionExtractionSerializer(Serializer):
    id: UUID
    relation: Optional[str]
    mime: str
    name: Optional[str]
    size: Optional[int] = None
    extractable: bool
    #: Why it cannot be processed: `not_pdf`, `external`, `no_file`; None when it can.
    reason: Optional[str]
    #: `none` when never queued, else the status of the latest run.
    status: str
    #: An active run is served to readers (possibly while a newer one is in progress).
    available: bool
    #: The active run is of an older pipeline version than the current one.
    outdated: bool
    runs: List[RunSerializer]
    task: Optional[QueueTaskSerializer] = None


class QueueDepthSerializer(Serializer):
    service: str
    queue: str
    #: Messages waiting in the broker; None when it could not be read.
    waiting: Optional[int]


class EntryExtractionSerializer(Serializer):
    entry_id: UUID
    #: `EVILFLOWERS_EXTRACTION_ENABLED`: off, nothing can be queued.
    enabled: bool
    pipeline_version: int
    #: Summary over the entry's acquisitions: `unsupported` (no PDF to process), `none`,
    #: `queued`, `running`, `done`, `failed`.
    status: str
    acquisitions: List[AcquisitionExtractionSerializer]
    queues: List[QueueDepthSerializer]
    page_count: Optional[int]
    table_of_contents: Optional[list]


class AdminRegionSerializer(Serializer):
    id: UUID
    run_id: UUID
    kind: str
    #: Page of the original file, 1-based.
    page: int
    seq: int
    bbox: Dict[str, float]
    caption: Optional[str]
    printed_caption: Optional[str]
    description: Optional[str]
    category: Optional[str]
    markdown: Optional[str]
    latex: Optional[str]
    provenance: dict
    image_path: Optional[str]
    image_url: Optional[str]


class AdminParagraphSerializer(Serializer):
    id: UUID
    page: int
    seq: int
    section: Optional[str]
    text: str
    sentences: int
    embedded: bool
    similar: int


def not_extractable_reason(acquisition: Acquisition) -> Optional[str]:
    if acquisition.mime != Acquisition.AcquisitionMIME.PDF:
        return "not_pdf"
    if acquisition.storage_backend != Acquisition.StorageBackend.LOCAL:
        return "external"
    if not acquisition.content:
        return "no_file"
    return None


def task_id(acquisition_id, pipeline_version: int) -> str:
    """The id `services.queue` sends the analyzer task under."""
    return f"extract:{acquisition_id}:v{pipeline_version}"


def run_stats(run: ExtractionRun) -> RunStatsSerializer:
    regions = run.regions.aggregate(
        figures=Count("id", filter=Q(kind=Region.Kind.FIGURE)),
        tables=Count("id", filter=Q(kind=Region.Kind.TABLE)),
        equations=Count("id", filter=Q(kind=Region.Kind.EQUATION)),
        with_image=Count("id", filter=Q(image_path__isnull=False) & ~Q(image_path="")),
        with_printed_caption=Count("id", filter=Q(printed_caption__isnull=False) & ~Q(printed_caption="")),
        with_description=Count("id", filter=Q(description__isnull=False) & ~Q(description="")),
        pages_with_regions=Count("page", distinct=True),
    )
    paragraphs = run.paragraphs.aggregate(
        paragraphs=Count("id"),
        paragraphs_embedded=Count("id", filter=Q(embedding__isnull=False)),
        sections=Count("section", distinct=True),
    )
    links = ParagraphLink.objects.filter(source__run=run).aggregate(
        links_next=Count("id", filter=Q(kind=ParagraphLink.Kind.NEXT)),
        links_similar=Count("id", filter=Q(kind=ParagraphLink.Kind.SIMILAR)),
    )
    characters = run.paragraphs.aggregate(total=Sum(Length("text")))["total"] or 0
    return RunStatsSerializer(
        **regions,
        **paragraphs,
        **links,
        sentences=Sentence.objects.filter(paragraph__run=run).count(),
        characters=characters,
        has_embedding=run.embedding is not None,
    )


def _broker_client():
    """The broker's Redis client, or None when the broker is not Redis."""
    try:
        with app.connection_for_read() as connection:
            client = getattr(connection.default_channel, "client", None)
            return client if client is not None and hasattr(client, "llen") else None
    except Exception:
        logger.warning("Extraction admin: the Celery broker could not be reached", exc_info=True)
        return None


def queue_depths(client) -> List[QueueDepthSerializer]:
    result = []
    for service, setting, default in SERVICE_QUEUES:
        name = getattr(settings, setting, default) if setting else default
        waiting = None
        if client is not None:
            try:
                waiting = int(client.llen(name))
            except Exception:
                waiting = None
        result.append(QueueDepthSerializer(service=service, queue=name, waiting=waiting))
    return result


def queued_task(client, acquisition_id, pipeline_version: int) -> QueueTaskSerializer:
    """Where the acquisition's analyzer task waits in the broker, if it does."""
    name = settings.EVILFLOWERS_EXTRACTION_QUEUE
    identifier = task_id(acquisition_id, pipeline_version)
    task = QueueTaskSerializer(task_id=identifier, queue=name)
    if client is None:
        return task
    try:
        messages = client.lrange(name, -QUEUE_SCAN_LIMIT, -1)
    except Exception:
        return task
    # Kombu pushes on the left and workers pop from the right: the last element is next.
    for index, raw in enumerate(reversed(messages)):
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        if identifier in text:
            return task.model_copy(update={"waiting": True, "position": index + 1})
    return task.model_copy(update={"waiting": False})


def summary_status(acquisitions: List[AcquisitionExtractionSerializer]) -> str:
    candidates = [item for item in acquisitions if item.extractable]
    if not candidates:
        return "unsupported"
    statuses = {item.status for item in candidates}
    # Work in progress first, then problems, then results.
    for status in ("running", "queued", "failed", "done"):
        if status in statuses:
            return status
    return "none"


class EntryExtractionView(SecuredView):
    @staticmethod
    def _entry(request, entry_id: UUID) -> Entry:
        entry = Entry.objects.select_related("catalog").filter(pk=entry_id).first()
        if entry is None:
            raise ProblemDetailException(
                _("Entry not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )
        if not has_object_permission("check_entry_manage", request.user, entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)
        return entry

    @staticmethod
    def _run(entry: Entry, request) -> Optional[ExtractionRun]:
        """`?run_id=` of the entry, else the active run of `?acquisition_id=` (or of any PDF), else the latest."""
        runs = ExtractionRun.objects.filter(acquisition__entry=entry)
        run_id = request.GET.get("run_id")
        acquisition_id = request.GET.get("acquisition_id")
        try:
            if run_id:
                return runs.filter(pk=UUID(run_id)).first()
            if acquisition_id:
                runs = runs.filter(acquisition_id=UUID(acquisition_id))
        except ValueError:
            raise ProblemDetailException(_("Invalid identifier"), status=HTTPStatus.BAD_REQUEST)
        return (
            runs.filter(is_active=True).order_by("-finished_at").first()
            or runs.order_by("-pipeline_version", "-created_at").first()
        )

    def _payload(self, request, entry: Entry) -> EntryExtractionSerializer:
        version = settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        client = _broker_client()
        acquisitions = []
        for acquisition in entry.acquisitions.order_by("created_at"):
            reason = not_extractable_reason(acquisition)
            runs = list(acquisition.extraction_runs.order_by("-pipeline_version", "-created_at"))
            serialized = []
            for run in runs:
                item = RunSerializer.model_validate(run)
                if run.started_at and run.finished_at:
                    item.duration = (run.finished_at - run.started_at).total_seconds()
                # Counts are cheap enough for the few runs an acquisition keeps (the active one and
                # the one in progress); the previous runs are deleted when a newer one finishes.
                item.stats = run_stats(run)
                serialized.append(item)
            active = next((run for run in runs if run.is_active), None)
            latest = runs[0] if runs else None
            size = None
            if acquisition.content and reason is None:
                try:
                    size = acquisition.content.size
                except Exception:
                    size = None
            task = None
            if latest and latest.status in (ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING):
                task = queued_task(client, acquisition.pk, latest.pipeline_version)
            acquisitions.append(
                AcquisitionExtractionSerializer(
                    id=acquisition.pk,
                    relation=acquisition.relation,
                    mime=acquisition.mime,
                    name=(acquisition.content.name.rsplit("/", 1)[-1] if acquisition.content else None),
                    size=size,
                    extractable=reason is None,
                    reason=reason,
                    status=latest.status if latest else "none",
                    available=active is not None,
                    outdated=bool(active and active.pipeline_version < version),
                    runs=serialized,
                    task=task,
                )
            )

        return EntryExtractionSerializer(
            entry_id=entry.pk,
            enabled=settings.EVILFLOWERS_EXTRACTION_ENABLED,
            pipeline_version=version,
            status=summary_status(acquisitions),
            acquisitions=acquisitions,
            queues=queue_depths(client),
            page_count=getattr(entry, "page_count", None),
            table_of_contents=getattr(entry, "table_of_contents", None),
        )


class EntryExtraction(EntryExtractionView):
    @openapi.metadata(
        description=(
            "Extraction (IP-018) of an entry for its managers: every run of every acquisition with its jobs, "
            "provenance, errors and counts (regions by kind, paragraphs, embeddings, sentences, links), the "
            "analyzer task's position in the broker queue while it waits, the depth of every service queue, "
            "and the entry's stored outline. Requires manage permission on the entry."
        ),
        tags=["Extraction"],
        summary="Get an entry's extraction",
    )
    @openapi.response(HTTPStatus.OK, "The extraction", EntryExtractionSerializer)
    def get(self, request, entry_id: UUID):
        entry = self._entry(request, entry_id)
        return SingleResponse(request, data=self._payload(request, entry))

    @openapi.metadata(
        description=(
            "Queue the entry's PDFs (or one of them, `acquisition_id`) for extraction by the current pipeline "
            "version. A run that is done is left alone unless `force`; a failed run is retried; a run left "
            "`queued` or `running` is sent again (services may have been down). Readers keep the active run "
            "until the new one finishes. 409 when extraction is disabled on this catalog."
        ),
        tags=["Extraction"],
        summary="Queue an entry for extraction",
    )
    @openapi.response(HTTPStatus.OK, "The extraction after queuing", EntryExtractionSerializer)
    def post(self, request, entry_id: UUID):
        entry = self._entry(request, entry_id)
        if not settings.EVILFLOWERS_EXTRACTION_ENABLED:
            raise ProblemDetailException(
                _("Extraction is disabled"),
                status=HTTPStatus.CONFLICT,
                detail=_("EVILFLOWERS_EXTRACTION_ENABLED is off on this catalog."),
            )
        form = QueueForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        acquisitions = entry.acquisitions.select_related("entry__catalog")
        if form.cleaned_data.get("acquisition_id"):
            acquisitions = acquisitions.filter(pk=form.cleaned_data["acquisition_id"])
        candidates = [acquisition for acquisition in acquisitions if not_extractable_reason(acquisition) is None]
        if not candidates:
            raise ProblemDetailException(
                _("Nothing to process"),
                status=HTTPStatus.BAD_REQUEST,
                detail=_("The entry has no PDF stored in the catalog."),
            )

        version = settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        for acquisition in candidates:
            stuck = ExtractionRun.objects.filter(
                acquisition=acquisition,
                pipeline_version=version,
                status__in=[ExtractionRun.Status.QUEUED, ExtractionRun.Status.RUNNING],
            ).exists()
            run = services.queue(acquisition, force=bool(form.cleaned_data.get("force")) or stuck)
            logger.info(
                "Extraction queued from the admin: entry_id=%s acquisition_id=%s run_id=%s user_id=%s",
                entry.pk,
                acquisition.pk,
                run and run.pk,
                request.user.pk,
            )

        return SingleResponse(request, data=self._payload(request, entry))


class EntryExtractionRegions(EntryExtractionView):
    @openapi.metadata(
        description=(
            "Every figure, table and equation of one run of the entry (default: the active run of any of its "
            "PDFs, else the latest run), in reading order, with bbox, captions, description, table markdown, "
            "equation LaTeX, provenance and the crop. `kind` narrows to one kind."
        ),
        tags=["Extraction"],
        summary="List an entry's extracted regions",
    )
    @openapi.response(HTTPStatus.OK, "Regions", AdminRegionSerializer)
    def get(self, request, entry_id: UUID):
        entry = self._entry(request, entry_id)
        run = self._run(entry, request)
        if run is None:
            return SingleResponse(request, data={"run_id": None, "items": []})
        regions = run.regions.order_by("page", "seq")
        kind = request.GET.get("kind")
        if kind:
            if kind not in Region.Kind.values:
                raise ProblemDetailException(_("Unknown region kind"), status=HTTPStatus.BAD_REQUEST)
            regions = regions.filter(kind=kind)

        items = []
        for region in regions:
            image_url = None
            if region.image_path:
                image_url = request.build_absolute_uri(
                    reverse("api:entry-extraction-region-image", kwargs={"entry_id": entry.pk, "region_id": region.pk})
                )
            items.append(
                AdminRegionSerializer(
                    id=region.pk,
                    run_id=run.pk,
                    kind=region.kind,
                    page=region.page,
                    seq=region.seq,
                    bbox=region.bbox,
                    caption=region.caption,
                    printed_caption=region.printed_caption,
                    description=region.description,
                    category=region.category,
                    markdown=region.markdown,
                    latex=region.latex,
                    provenance=region.provenance or {},
                    image_path=region.image_path,
                    image_url=image_url,
                ).model_dump(mode="json")
            )
        return SingleResponse(request, data={"run_id": str(run.pk), "items": items})


class EntryExtractionParagraphs(EntryExtractionView):
    @openapi.metadata(
        description=(
            "Paragraphs of one run of the entry (same run choice as the regions), in reading order and paged "
            "with `page` (1-based) and `limit`, or only those on PDF page `pdf_page`, or matching `query`. "
            "Each carries its section, sentence count, whether it is embedded and its similar-paragraph links."
        ),
        tags=["Extraction"],
        summary="List an entry's extracted paragraphs",
    )
    @openapi.response(HTTPStatus.OK, "Paragraphs", AdminParagraphSerializer)
    def get(self, request, entry_id: UUID):
        entry = self._entry(request, entry_id)
        run = self._run(entry, request)
        empty = {"run_id": None, "items": [], "total": 0, "page": 1, "pages": 1, "limit": PARAGRAPHS_DEFAULT_LIMIT}
        if run is None:
            return SingleResponse(request, data=empty)

        try:
            limit = max(1, min(int(request.GET.get("limit") or PARAGRAPHS_DEFAULT_LIMIT), PARAGRAPHS_MAX_LIMIT))
            page = max(1, int(request.GET.get("page") or 1))
            pdf_page = int(request.GET["pdf_page"]) if request.GET.get("pdf_page") else None
        except ValueError:
            raise ProblemDetailException(_("Invalid paging"), status=HTTPStatus.BAD_REQUEST)

        paragraphs = run.paragraphs.order_by("seq")
        if pdf_page:
            paragraphs = paragraphs.filter(page=pdf_page)
        query = (request.GET.get("query") or "").strip()
        if query:
            paragraphs = paragraphs.filter(text__icontains=query)

        total = paragraphs.count()
        pages = max(1, -(-total // limit))
        rows = paragraphs.annotate(
            sentence_count=Count("sentences", distinct=True),
            similar_count=Count("links", filter=Q(links__kind=ParagraphLink.Kind.SIMILAR), distinct=True),
            embedded=ExpressionWrapper(Q(embedding__isnull=False), output_field=BooleanField()),
        ).defer("embedding")[(page - 1) * limit : page * limit]
        items = [
            AdminParagraphSerializer(
                id=row.pk,
                page=row.page,
                seq=row.seq,
                section=row.section,
                text=row.text,
                sentences=row.sentence_count,
                embedded=bool(row.embedded),
                similar=row.similar_count,
            ).model_dump(mode="json")
            for row in rows
        ]
        return SingleResponse(
            request,
            data={"run_id": str(run.pk), "items": items, "total": total, "page": page, "pages": pages, "limit": limit},
        )


class EntryExtractionRegionImage(EntryExtractionView):
    @openapi.metadata(description="The crop of a region of any run of the entry, for its managers.", tags=["Files"])
    def get(self, request, entry_id: UUID, region_id: UUID):
        entry = self._entry(request, entry_id)
        region = Region.objects.filter(pk=region_id, run__acquisition__entry=entry).first()
        storage = get_storage()
        if region is None or not region.image_path or not storage.exists(region.image_path):
            raise ProblemDetailException(_("Region image not found"), status=HTTPStatus.NOT_FOUND)
        response = FileResponse(
            storage.open(region.image_path, "rb"),
            content_type=mimetypes.guess_type(region.image_path)[0] or "image/webp",
        )
        response["Cache-Control"] = "private, max-age=3600"
        return response


#: Values of `EntryFilter.extraction_status` and of the `extraction_status` annotation.
ENTRY_EXTRACTION_STATUSES = ("unsupported", "none", "queued", "running", "done", "failed")


def annotate_entries(queryset):
    """Extraction columns of the admin's entry list, sortable by `order_by`.

    - `extraction_status`: the status of the entry's newest run; `none` when it has a
      PDF that was never queued, `unsupported` when it has nothing to process.
    - `extraction_regions`, `extraction_paragraphs`: counts of the active runs.
    - `extraction_finished_at`: when the newest active run finished.
    """
    from django.db.models import Case, CharField, Exists, IntegerField, Max, OuterRef, Subquery, Value, When
    from django.db.models.functions import Coalesce

    from apps.extraction.models import Paragraph

    if "extraction_status" in queryset.query.annotations:
        return queryset

    entry = OuterRef("pk")
    latest_status = Subquery(
        ExtractionRun.objects.filter(acquisition__entry=entry).order_by("-created_at").values("status")[:1],
        output_field=CharField(),
    )
    extractable = Exists(
        Acquisition.objects.filter(
            entry=entry,
            mime=Acquisition.AcquisitionMIME.PDF,
            storage_backend=Acquisition.StorageBackend.LOCAL,
        )
        .exclude(content="")
        .exclude(content__isnull=True)
    )

    def count(model, path):
        return Coalesce(
            Subquery(
                model.objects.filter(**{f"{path}acquisition__entry": entry, f"{path}is_active": True})
                .order_by()
                .values(f"{path}acquisition__entry")
                .annotate(total=Count("id"))
                .values("total")[:1],
                output_field=IntegerField(),
            ),
            0,
        )

    return queryset.annotate(
        extraction_status=Coalesce(
            latest_status,
            Case(When(extractable, then=Value("none")), default=Value("unsupported"), output_field=CharField()),
        ),
        extraction_regions=count(Region, "run__"),
        extraction_paragraphs=count(Paragraph, "run__"),
        extraction_finished_at=Subquery(
            ExtractionRun.objects.filter(acquisition__entry=entry, is_active=True)
            .order_by()
            .values("acquisition__entry")
            .annotate(last=Max("finished_at"))
            .values("last")[:1]
        ),
    )


def wants_entry_columns(request) -> bool:
    """The entry list annotates extraction only when asked to show, filter or sort by it."""
    return (
        request.GET.get("extraction") in ("1", "true")
        or "extraction_" in request.GET.get("order_by", "")
        or bool(request.GET.get("extraction_status"))
    )

"""Extraction endpoints (IP-018).

Ingestion (`/extraction/v1/runs/...`) is called by the `evilflowers-*-service`
containers, authenticated like every other API call — a bearer API key — of a
user holding `extraction.ingest_extraction` (a dedicated service user, Q12).

The reader endpoints (`/api/v1/user-acquisitions/{id}/regions`, `.../extraction`
and the crop under `/data/v1/`) make the same checks as the download of that
copy and speak the reader's page numbers.
"""

import mimetypes
from http import HTTPStatus
from typing import List, Optional
from uuid import UUID

from django.db.models import Count, Q
from django.http import FileResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import SingleResponse
from apps.assistant.pdf import reader_source_pages
from apps.core.errors import (
    AuthorizationException,
    DetailType,
    ProblemDetailException,
    UnauthorizedException,
    ValidationException,
)
from apps.core.models import UserAcquisition
from apps.core.views import SecuredView
from apps.extraction import services
from apps.extraction.forms import JobReportForm, ParagraphsForm, RegionsForm, StartRunForm
from apps.extraction.models import ExtractionRun, Region
from apps.extraction.serializers import ExtractionRunSerializer, ExtractionStatusSerializer, RegionSerializer
from apps.files.storage import get_storage
from apps.files.views import _check_ip_block
from apps.openapi.types import ParameterLocation

#: Job name the analyzer reports under when the document itself cannot be processed.
ANALYZER_JOB = "analyzer"


class IngestionView(SecuredView):
    def _run(self, request, run_id: UUID) -> ExtractionRun:
        if not request.user.is_authenticated:
            raise UnauthorizedException(detail=_("Extraction services must authenticate"))
        if not request.user.has_perm("extraction.ingest_extraction"):
            raise AuthorizationException(request)
        run = (
            ExtractionRun.objects.select_related("acquisition", "acquisition__entry__catalog")
            .filter(pk=run_id)
            .first()
        )
        if run is None:
            raise ProblemDetailException(_("Extraction run not found"), status=HTTPStatus.NOT_FOUND)
        return run

    @staticmethod
    def _conflict(error: services.RunNotRunning) -> ProblemDetailException:
        return ProblemDetailException(
            _("The extraction run is not running"),
            status=HTTPStatus.CONFLICT,
            detail=_("Run status is %(status)s.") % {"status": error},
        )


class RunDetail(IngestionView):
    @openapi.metadata(
        description="An extraction run as the services see it: status, jobs and the storage folder for its crops.",
        tags=["Extraction"],
        summary="Get an extraction run",
    )
    @openapi.response(HTTPStatus.OK, "The run", ExtractionRunSerializer.Base)
    def get(self, request, run_id: UUID):
        run = self._run(request, run_id)
        return SingleResponse(request, data=ExtractionRunSerializer.Base.model_validate(run))


class RunStart(IngestionView):
    @openapi.metadata(
        description=(
            "Called by the analyzer once it has opened the document: page count, page sizes in PDF points "
            "and the jobs it dispatched. The run is `running` from here until every job has reported."
        ),
        tags=["Extraction"],
        summary="Start an extraction run",
    )
    @openapi.response(HTTPStatus.OK, "The run", ExtractionRunSerializer.Base)
    def post(self, request, run_id: UUID):
        run = self._run(request, run_id)
        form = StartRunForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            run = services.start(run, **form.cleaned_data)
        except services.RunNotRunning as error:
            raise self._conflict(error) from error

        return SingleResponse(request, data=ExtractionRunSerializer.Base.model_validate(run))


class RunRegions(IngestionView):
    @openapi.metadata(
        description=(
            "Add figures, tables or equations found by a service. Boxes are page fractions with a top-left "
            "origin on the original file's 1-based page; `image_path` is the crop's storage name. Rejected "
            "with 409 unless the run is `running`."
        ),
        tags=["Extraction"],
        summary="Add regions to an extraction run",
    )
    def post(self, request, run_id: UUID):
        run = self._run(request, run_id)
        form = RegionsForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            count = services.add_regions(run, form.cleaned_data["regions"])
        except services.RunNotRunning as error:
            raise self._conflict(error) from error

        return SingleResponse(request, data={"created": count}, status=HTTPStatus.CREATED)


class RunParagraphs(IngestionView):
    @openapi.metadata(
        description=(
            "Add a batch of paragraphs with their sentences and embeddings (IP-018 Phase 2b). Links refer to "
            "paragraphs by `seq` and may point back at earlier batches. Rejected with 409 unless the run is "
            "`running`, or when the batch repeats a `seq` the run already holds."
        ),
        tags=["Extraction"],
        summary="Add paragraphs to an extraction run",
    )
    def post(self, request, run_id: UUID):
        run = self._run(request, run_id)
        form = ParagraphsForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        try:
            count = services.add_paragraphs(run, form.cleaned_data["paragraphs"], form.cleaned_data.get("links") or [])
        except services.RunNotRunning as error:
            raise self._conflict(error) from error
        except services.DuplicateParagraphs as error:
            raise ProblemDetailException(
                _("The extraction run already holds these paragraphs"),
                status=HTTPStatus.CONFLICT,
                detail=_("A paragraph with the same seq was stored before; retrying the batch will not help."),
            ) from error

        return SingleResponse(request, data={"created": count}, status=HTTPStatus.CREATED)


class RunJob(IngestionView):
    @openapi.metadata(
        description=(
            "A service reports its job of the run `done` or `failed`. When the last job reports, the run "
            "becomes the acquisition's active run and the previous one is deleted, or fails if any job "
            "failed. The analyzer reports job `analyzer` as `failed` when the document cannot be processed."
        ),
        tags=["Extraction"],
        summary="Report an extraction job",
    )
    @openapi.response(HTTPStatus.OK, "The run", ExtractionRunSerializer.Base)
    def post(self, request, run_id: UUID, job: str):
        run = self._run(request, run_id)
        form = JobReportForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        failed = form.cleaned_data["status"] == "failed"
        if job == ANALYZER_JOB and failed:
            services.fail(run, form.cleaned_data.get("error") or _("The analyzer failed"))
        else:
            try:
                services.report_job(
                    run,
                    job,
                    failed=failed,
                    error=form.cleaned_data.get("error"),
                    provenance=form.cleaned_data.get("provenance") or {},
                )
            except services.RunNotRunning as error:
                raise self._conflict(error) from error

        run.refresh_from_db()
        return SingleResponse(request, data=ExtractionRunSerializer.Base.model_validate(run))


class ReaderView(SecuredView):
    """A reader's copy, with the checks `UserAcquisitionDownload` makes."""

    def _user_acquisition(self, request, user_acquisition_id: UUID) -> UserAcquisition:
        user_acquisition = (
            UserAcquisition.objects.select_related("acquisition", "acquisition__entry", "user")
            .filter(pk=user_acquisition_id)
            .first()
        )
        if user_acquisition is None:
            raise ProblemDetailException(
                _("User acquisition not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )
        if user_acquisition.type == UserAcquisition.UserAcquisitionType.PERSONAL:
            if not has_object_permission("check_user_acquisition_read", request.user, user_acquisition):
                raise AuthorizationException(request)
        _check_ip_block(request, user_acquisition.acquisition.entry)
        return user_acquisition

    @staticmethod
    def _sources(user_acquisition: UserAcquisition, run: ExtractionRun) -> List[Optional[int]]:
        return reader_source_pages(run.page_count or 0, user_acquisition.range, user_acquisition.acquisition.mime)

    def _region(self, user_acquisition: UserAcquisition, region_id: UUID) -> tuple[Region, int]:
        """A region of the active run on a page the reader may see, with its reader page."""
        run = services.active_run(user_acquisition.acquisition_id)
        region = run and run.regions.filter(pk=region_id).first()
        reader_page = region and _reader_page(self._sources(user_acquisition, run), region.page)
        if not reader_page:
            raise ProblemDetailException(_("Region not found"), status=HTTPStatus.NOT_FOUND)
        return region, reader_page


def _reader_page(sources: List[Optional[int]], source: int) -> Optional[int]:
    try:
        return sources.index(source) + 1
    except ValueError:
        return None


def _serialize(request, user_acquisition: UserAcquisition, region: Region, page: int) -> RegionSerializer:
    image_url = None
    if region.image_path:
        image_url = request.build_absolute_uri(
            reverse(
                "files:user-acquisition-region-image",
                kwargs={"user_acquisition_id": user_acquisition.pk, "region_id": region.pk},
            )
        )
    return RegionSerializer(
        id=region.pk,
        kind=region.kind,
        page=page,
        seq=region.seq,
        bbox=region.bbox,
        caption=region.caption,
        printed_caption=region.printed_caption,
        description=region.description,
        category=region.category,
        markdown=region.markdown,
        latex=region.latex,
        image_url=image_url,
    )


class UserAcquisitionRegions(ReaderView):
    @openapi.metadata(
        description=(
            "Figures, tables and equations on a page of the reader's copy, in the reader's page numbering "
            "(the viewer's). Without `page`, every region of the copy. Pages outside the copy's range are "
            "never returned. Empty while the publication has not been processed; see `.../extraction`."
        ),
        tags=["Extraction"],
        summary="List regions of a reader's copy",
    )
    @openapi.parameter(
        name="page", param_type=int, location=ParameterLocation.QUERY, description="Reader page", required=False
    )
    def get(self, request, user_acquisition_id: UUID):
        user_acquisition = self._user_acquisition(request, user_acquisition_id)
        run = services.active_run(user_acquisition.acquisition_id)
        if run is None:
            return SingleResponse(request, data={"items": []})

        sources = self._sources(user_acquisition, run)
        regions = run.regions.all()
        page = request.GET.get("page")
        if page:
            try:
                source = sources[int(page) - 1] if int(page) >= 1 else None
            except (ValueError, IndexError):
                source = None
            if source is None:
                return SingleResponse(request, data={"items": []})
            regions = regions.filter(page=source)

        items = []
        for region in regions:
            reader_page = _reader_page(sources, region.page)
            if reader_page:
                items.append(_serialize(request, user_acquisition, region, reader_page).model_dump(mode="json"))
        return SingleResponse(request, data={"items": items})


class UserAcquisitionRegionDetail(ReaderView):
    @openapi.metadata(
        description="One region of a reader's copy: caption and category, table markdown or equation LaTeX.",
        tags=["Extraction"],
        summary="Get a region",
    )
    @openapi.response(HTTPStatus.OK, "The region", RegionSerializer)
    def get(self, request, user_acquisition_id: UUID, region_id: UUID):
        user_acquisition = self._user_acquisition(request, user_acquisition_id)
        region, page = self._region(user_acquisition, region_id)
        return SingleResponse(request, data=_serialize(request, user_acquisition, region, page))


class UserAcquisitionRegionImage(ReaderView):
    @openapi.metadata(description="The crop of a region, as an image.", tags=["Files"])
    def get(self, request, user_acquisition_id: UUID, region_id: UUID):
        user_acquisition = self._user_acquisition(request, user_acquisition_id)
        region, _page = self._region(user_acquisition, region_id)
        storage = get_storage()
        if not region.image_path or not storage.exists(region.image_path):
            raise ProblemDetailException(_("Region image not found"), status=HTTPStatus.NOT_FOUND)

        response = FileResponse(
            storage.open(region.image_path, "rb"),
            content_type=mimetypes.guess_type(region.image_path)[0] or "image/webp",
        )
        # Crops are parts of restricted publications: never cached by shared caches.
        response["Cache-Control"] = "private, max-age=3600"
        return response


class UserAcquisitionExtraction(ReaderView):
    @openapi.metadata(
        description=(
            'Extraction status of a reader\'s copy, so a client can tell "nothing found" from "not '
            "processed yet\": the latest run's status and the counts of the active run."
        ),
        tags=["Extraction"],
        summary="Get extraction status",
    )
    @openapi.response(HTTPStatus.OK, "The status", ExtractionStatusSerializer)
    def get(self, request, user_acquisition_id: UUID):
        user_acquisition = self._user_acquisition(request, user_acquisition_id)
        latest = (
            ExtractionRun.objects.filter(acquisition_id=user_acquisition.acquisition_id)
            .order_by("-pipeline_version", "-created_at")
            .first()
        )
        active = services.active_run(user_acquisition.acquisition_id)
        data = ExtractionStatusSerializer(
            status=latest.status if latest else "none",
            pipeline_version=latest.pipeline_version if latest else None,
            available=active is not None,
        )
        if active is not None:
            counts = active.regions.aggregate(
                figures=Count("id", filter=Q(kind=Region.Kind.FIGURE)),
                tables=Count("id", filter=Q(kind=Region.Kind.TABLE)),
                equations=Count("id", filter=Q(kind=Region.Kind.EQUATION)),
            )
            data = data.model_copy(update={**counts, "paragraphs": active.paragraphs.count()})
        return SingleResponse(request, data=data)

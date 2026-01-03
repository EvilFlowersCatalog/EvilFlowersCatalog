import json
import os
from http import HTTPStatus
from urllib.request import Request, urlopen
from uuid import UUID

from django.db import transaction
from django.http import HttpResponse
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.acquisitions import AcquisitionFilter
from apps.api.forms.entries import AcquisitionMetaForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.errors import ProblemDetailException, ValidationException
from apps.core.models import Acquisition
from apps.core.models.entry import Entry
from apps.core.views import SecuredView


def _env(name: str) -> str | None:
    v = os.getenv(name)
    return v if v else None


def _dv_base_url() -> str:
    base = _env("DATAVERSE_BASE_URL")
    if not base:
        raise ProblemDetailException(_("DATAVERSE_BASE_URL is not configured"), status=HTTPStatus.INTERNAL_SERVER_ERROR)
    return base.rstrip("/")


def _dv_api_token() -> str | None:
    return _env("DATAVERSE_API_TOKEN")


def _workflow_secret() -> str | None:
    return _env("DATAVERSE_WORKFLOW_SECRET")


def _dv_get_latest_published_files(dataset_id: str) -> list[dict]:
    url = f"{_dv_base_url()}/api/datasets/{dataset_id}/versions/:latest-published"

    headers = {"Accept": "application/json"}
    token = _dv_api_token()
    if token:
        headers["X-Dataverse-key"] = token

    req = Request(url=url, method="GET", headers=headers)
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        data = json.loads(raw)

    dv_data = data.get("data") if isinstance(data, dict) else None
    files = dv_data.get("files") if isinstance(dv_data, dict) else None
    return files if isinstance(files, list) else []


def _dv_file_id(item: dict) -> int | None:
    df = item.get("dataFile") if isinstance(item, dict) else None
    if isinstance(df, dict):
        fid = df.get("id")
        return fid if isinstance(fid, int) else None
    return None


def _dv_content_type(item: dict) -> str | None:
    df = item.get("dataFile") if isinstance(item, dict) else None
    if isinstance(df, dict):
        ct = df.get("contentType")
        return ct if isinstance(ct, str) and ct else None
    return None


def _dv_file_url(file_id: int) -> str:
    return f"{_dv_base_url()}/api/access/datafile/{file_id}?format=original"


def _validate_lookup_field(field: str) -> str:
    f = str(field)
    if "__" in f or not f.isidentifier():
        raise ProblemDetailException(_("Invalid entry_lookup_field"), status=HTTPStatus.BAD_REQUEST)
    return f


def _resolve_entry(payload: dict) -> Entry:
    entry_id = payload.get("entry_id")
    if entry_id:
        try:
            entry_uuid = UUID(str(entry_id))
        except Exception as e:
            raise ProblemDetailException(_("Invalid entry_id"), status=HTTPStatus.BAD_REQUEST, previous=e)

        try:
            return Entry.objects.select_related("catalog").get(pk=entry_uuid)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Entry not found"), status=HTTPStatus.NOT_FOUND)

    global_id = payload.get("global_id")
    lookup_field_raw = payload.get("entry_lookup_field")
    if global_id and lookup_field_raw:
        lookup_field = _validate_lookup_field(lookup_field_raw)
        entry = Entry.objects.select_related("catalog").filter(**{lookup_field: global_id}).first()
        if entry:
            return entry

    catalog_id = payload.get("catalog_id") or payload.get("target_catalog_id")
    if not catalog_id:
        raise ProblemDetailException(
            _("Cannot resolve Entry: provide entry_id, or provide global_id + entry_lookup_field, or provide catalog_id/target_catalog_id to allow auto-create"),
            status=HTTPStatus.BAD_REQUEST,
        )

    try:
        catalog_uuid = UUID(str(catalog_id))
    except Exception as e:
        raise ProblemDetailException(_("Invalid catalog_id"), status=HTTPStatus.BAD_REQUEST, previous=e)

    title = payload.get("title") or "Untitled"
    entry = Entry(catalog_id=catalog_uuid, title=title)

    if global_id and lookup_field_raw:
        lookup_field = _validate_lookup_field(lookup_field_raw)
        setattr(entry, lookup_field, global_id)

    entry.save()
    return entry


class AcquisitionManagement(SecuredView):
    @openapi.metadata(
        description="Create a new acquisition file for an entry. Acquisitions represent downloadable content (PDF, EPUB, etc.) with associated metadata, pricing information, and access controls. Note: This endpoint is currently not implemented.",
        tags=["Acquisitions"],
        summary="Create acquisition file",
    )
    def post(self, request):
        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            payload = json.loads(raw)
        except Exception as e:
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=e)

        expected_secret = _workflow_secret()
        if expected_secret:
            provided_secret = payload.get("secret") or request.headers.get("X-Dataverse-Secret")
            if provided_secret != expected_secret:
                raise ProblemDetailException(_("Invalid secret"), status=HTTPStatus.FORBIDDEN)

        dataset_id = payload.get("dataset_id")
        if dataset_id is None:
            raise ProblemDetailException(_("Missing required field: dataset_id"), status=HTTPStatus.BAD_REQUEST)

        entry = _resolve_entry(payload)

        is_internal = bool(expected_secret)
        if not is_internal:
            if not has_object_permission("check_catalog_manage", request.user, entry.catalog):
                raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        files = _dv_get_latest_published_files(str(dataset_id))

        supported = {
            "application/pdf",
            "application/epub+zip",
            "application/x-mobipocket-ebook",
            "application/webpub+zip",
        }

        created = []
        skipped = 0

        with transaction.atomic():
            for f in files:
                mime = _dv_content_type(f)
                fid = _dv_file_id(f)
                if not mime or fid is None:
                    skipped += 1
                    continue
                if mime not in supported:
                    skipped += 1
                    continue

                file_url = _dv_file_url(fid)

                if Acquisition.objects.filter(entry=entry, file_url=file_url).exists():
                    skipped += 1
                    continue

                acq = Acquisition.objects.create(
                    entry=entry,
                    mime=mime,
                    file_url=file_url,
                )
                created.append(acq)

        print(
            f"[DATAVERSE-ACQ] dataset_id={dataset_id} global_id={payload.get('global_id')} entry_id={entry.pk} created={len(created)} skipped={skipped}",
            flush=True,
        )

        if not created:
            return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")

        return SingleResponse(
            request,
            data={
                "created": len(created),
                "skipped": skipped,
                "acquisitions": [
                    AcquisitionSerializer.Detailed.model_validate(a, context={"request": request}) for a in created
                ],
            },
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description="Retrieve a paginated list of acquisition files with filtering options. Supports filtering by entry, file type, media type, and availability status. Acquisitions represent downloadable content associated with catalog entries.",
        tags=["Acquisitions"],
        summary="List acquisition files",
    )
    def get(self, request):
        acquisitions = AcquisitionFilter(request.GET, queryset=Acquisition.objects.all(), request=request).qs
        return PaginationResponse(
            request, acquisitions, serializer=AcquisitionSerializer.Base, serializer_context={"request": request}
        )


class AcquisitionDetail(SecuredView):
    @staticmethod
    def _get_acquisition(request, acquisition_id: UUID, checker: str = "check_catalog_manage") -> Acquisition:
        try:
            acquisition = Acquisition.objects.select_related("entry__catalog").get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(_("Acquisition not found"), status=HTTPStatus.NOT_FOUND)

        if not has_object_permission(checker, request.user, acquisition.entry.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return acquisition

    @openapi.metadata(
        description="Retrieve detailed information about a specific acquisition file including its metadata, file properties, pricing information, and download availability.",
        tags=["Acquisitions"],
        summary="Get acquisition details",
    )
    def get(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id, "check_catalog_read")

        return SingleResponse(
            request, data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request})
        )

    @openapi.metadata(
        description="Update acquisition metadata including type, relation, pricing information, and availability settings. The actual file content is immutable - only metadata can be modified through the API. This allows for price updates, availability changes, and metadata corrections.",
        tags=["Acquisitions"],
        summary="Update acquisition metadata",
    )
    def put(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id, "check_catalog_manage")
        form = AcquisitionMetaForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(acquisition)
        acquisition.save()

        return SingleResponse(
            request,
            data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request}),
            status=HTTPStatus.OK,
        )

    @openapi.metadata(
        description="Remove an acquisition file from the catalog. This deletes the database record immediately, while the actual file removal occurs later during the orphaned files cleanup process. This action is irreversible.",
        tags=["Acquisitions"],
        summary="Delete acquisition file",
    )
    def delete(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id)
        acquisition.delete()

        return SingleResponse(request)

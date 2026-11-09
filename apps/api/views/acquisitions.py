import json
from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.acquisitions import AcquisitionFilter
from apps.api.forms.entries import AcquisitionMetaForm
from apps.api.services.entry import InvalidAcquisitionFile, attach_acquisition
from apps.core.errors import DetailType, ProblemDetailException, ValidationException
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.models import Acquisition, Entry, Price
from apps.core.views import SecuredView


def create_acquisition(request, entry: Entry) -> Acquisition:
    """A new acquisition of `entry` from a multipart request: `content` (the file) and `metadata` (JSON).

    Shared by `POST /api/v1/acquisitions` and `POST /api/v1/catalogs/{c}/entries/{e}`. The caller checks that
    the requester may manage the entry.
    """
    try:
        metadata = json.loads(request.POST.get("metadata", "{}"))
    except json.JSONDecodeError as e:
        raise ProblemDetailException(
            title=_("Unable to parse metadata for request file"),
            status=HTTPStatus.BAD_REQUEST,
            previous=e,
        )

    form = AcquisitionMetaForm(metadata, request)
    if not form.is_valid():
        raise ValidationException(form)

    if "content" not in request.FILES:
        raise ProblemDetailException(_("Missing `content` file"), status=HTTPStatus.BAD_REQUEST)

    try:
        acquisition = attach_acquisition(
            entry,
            request.FILES["content"],
            mime=request.FILES["content"].content_type,
            relation=form.cleaned_data.get("relation"),
        )
    except InvalidAcquisitionFile as error:
        # #112: an empty or mislabelled file is refused before it is stored.
        raise ProblemDetailException(str(error), status=error.status, detail_type=DetailType.VALIDATION_ERROR)

    for price in form.cleaned_data.get("prices", []):
        Price.objects.create(acquisition=acquisition, currency=price["currency_code"], value=price["value"])
    return acquisition


class AcquisitionManagement(SecuredView):
    @openapi.metadata(
        description=(
            "Create a new acquisition file for an entry (#7). Multipart form: `entry_id`, `content` (the file) and "
            "optional `metadata` (JSON: `relation`, `prices`). The same as "
            "`POST /api/v1/catalogs/{catalog_id}/entries/{entry_id}`; requires `manage` on the entry's catalog. "
            "An empty file, or one whose first bytes contradict its type, is refused (400 / 415)."
        ),
        tags=["Acquisitions"],
        summary="Create acquisition file",
    )
    @openapi.response(HTTPStatus.CREATED, "Acquisition created", AcquisitionSerializer.Detailed)
    def post(self, request):
        try:
            entry_id = UUID(str(request.POST.get("entry_id") or ""))
        except ValueError:
            raise ProblemDetailException(
                _("Parameter entry_id is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        entry = Entry.objects.select_related("catalog").filter(pk=entry_id).first()
        if entry is None or not has_object_permission("check_catalog_read", request.user, entry.catalog):
            raise ProblemDetailException(_("Entry not found"), status=HTTPStatus.NOT_FOUND)
        if not has_object_permission("check_catalog_manage", request.user, entry.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        acquisition = create_acquisition(request, entry)
        return SingleResponse(
            request,
            data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request}),
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
        description="Remove an acquisition file from the catalog. This deletes the database record and, once the deletion commits, the stored file with it. This action is irreversible.",
        tags=["Acquisitions"],
        summary="Delete acquisition file",
    )
    def delete(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id)
        acquisition.delete()

        return SingleResponse(request)

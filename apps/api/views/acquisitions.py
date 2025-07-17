from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.acquisitions import AcquisitionFilter
from apps.api.forms.entries import AcquisitionMetaForm
from apps.core.errors import ProblemDetailException, ValidationException
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.models import Acquisition
from apps.core.views import SecuredView


class AcquisitionManagement(SecuredView):
    @openapi.metadata(
        description="Create a new acquisition file for an entry. Acquisitions represent downloadable content (PDF, EPUB, etc.) with associated metadata, pricing information, and access controls. Note: This endpoint is currently not implemented.",
        tags=["Acquisitions"],
        summary="Create acquisition file",
    )
    def post(self, request):
        raise ProblemDetailException(_("Not implemented"), status=HTTPStatus.NOT_IMPLEMENTED)

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

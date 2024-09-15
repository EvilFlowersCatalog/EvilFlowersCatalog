from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.acquisitions import AcquisitionFilter
from apps.api.forms.entries import AcquisitionMetaForm, AcquisitionForm
from apps.core.errors import ProblemDetailException, ValidationException
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.models import Acquisition
from apps.core.views import SecuredView


class AcquisitionManagement(SecuredView):
    @openapi.metadata(description="Create Acquisition", tags=["Acquisitions"])
    def post(self, request):
        raise ProblemDetailException(_("Not implemented"), status=HTTPStatus.NOT_IMPLEMENTED)

    @openapi.metadata(description="List Acquisitions", tags=["Acquisitions"])
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

    @openapi.metadata(description="Get Acquisition detail", tags=["Acquisitions"])
    def get(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id, "check_catalog_read")

        return SingleResponse(
            request, data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request})
        )

    @openapi.metadata(
        description="Content of Acquisition is imutable from the API users perspective. You can only Acquisition "
        "metadata such as it's type (relation) or pricing.",
        tags=["Acquisitions"],
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
        description="Delete acquisition from the database. The related static files will be removed later during the "
        "orphans removal process - check GitHub docs.",
        tags=["Acquisitions"],
    )
    def delete(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id)
        acquisition.delete()

        return SingleResponse(request)

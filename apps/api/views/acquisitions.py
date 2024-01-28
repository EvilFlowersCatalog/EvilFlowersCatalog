from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.core.errors import ProblemDetailException
from apps.api.response import SingleResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.models import Acquisition
from apps.core.views import SecuredView


class AcquisitionManagement(SecuredView):
    def post(self, request):
        ...

    def get(self, request):
        ...


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

    def get(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id, "check_catalog_read")

        return SingleResponse(request, AcquisitionSerializer.Detailed.model_validate(acquisition))

from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.api.response import SingleResponse
from apps.api.serializers.entries import AcquisitionSerializer
from apps.core.models import Acquisition
from apps.core.views import SecuredView


class AcquisitionDetail(SecuredView):
    @staticmethod
    def _get_acquisition(request, acquisition_id: UUID) -> Acquisition:
        try:
            acquisition = Acquisition.objects.get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(request, _("Acquisition not found"), status=HTTPStatus.NOT_FOUND)

        if not request.user.is_superuser and acquisition.entry.creator_id != request.user_id:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return acquisition

    def get(self, request, acquisition_id: UUID):
        acquisition = self._get_acquisition(request, acquisition_id)

        return SingleResponse(request, acquisition, serializer=AcquisitionSerializer.Detailed)

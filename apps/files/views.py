import uuid
from http import HTTPStatus

from django.http import FileResponse
from django.utils.translation import gettext as _

from apps.api.errors import ProblemDetailException
from apps.api.views.base import SecuredView
from apps.core.models import Acquisition


class AcquisitionDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, acquisition_id: uuid.UUID):
        try:
            acquisition = Acquisition.objects.get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(request, _("Acquisition not found"), status=HTTPStatus.NOT_FOUND)

        if acquisition.relation != Acquisition.AcquisitionType.ACQUISITION.OPEN_ACCESS:
            self._authenticate(request)

        return FileResponse(acquisition.content, as_attachment=True, filename=acquisition.entry.title)

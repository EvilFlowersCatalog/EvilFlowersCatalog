import uuid
from http import HTTPStatus

from django.http import FileResponse
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.api.views.base import SecuredView
from apps.core.models import Acquisition, Entry


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


class EntryImageDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, image__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(request, _("Entry image not found"), status=HTTPStatus.NOT_FOUND)

        return FileResponse(entry.image, as_attachment=True, filename=entry.title)

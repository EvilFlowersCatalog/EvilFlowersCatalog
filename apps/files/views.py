import uuid
from http import HTTPStatus
from mimetypes import guess_extension

from django.conf import settings
from django.http import FileResponse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from redis import Redis

from apps.core.errors import ProblemDetailException
from apps.core.models import Acquisition, Entry
from apps.core.views import SecuredView


class AcquisitionDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, acquisition_id: uuid.UUID):
        try:
            acquisition = Acquisition.objects.get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(request, _("Acquisition not found"), status=HTTPStatus.NOT_FOUND)

        if acquisition.relation != Acquisition.AcquisitionType.ACQUISITION.OPEN_ACCESS:
            self._authenticate(request)

        redis = Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DATABASE
        )

        if request.user.is_anonymous:
            user_id = uuid.uuid4()
        else:
            user_id = request.user.pk

        redis.pfadd(f"popularity:{acquisition.entry_id}", str(user_id))

        sanitized_filename = f"{slugify(acquisition.entry.title.lower())}{guess_extension(acquisition.mime)}"

        return FileResponse(acquisition.content, as_attachment=True, filename=sanitized_filename)


class EntryImageDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, image__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(request, _("Entry image not found"), status=HTTPStatus.NOT_FOUND)

        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime)}"

        return FileResponse(entry.image, filename=sanitized_filename)

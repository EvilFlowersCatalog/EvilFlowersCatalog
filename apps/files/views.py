import base64
import uuid
from http import HTTPStatus
from mimetypes import guess_extension

from django.conf import settings
from django.http import FileResponse
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission
from redis import Redis

from apps.api.response import SingleResponse
from apps.core.errors import ProblemDetailException
from apps.core.models import Acquisition, Entry, UserAcquisition
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

        redis.pfadd(f"evilflowers:popularity:{acquisition.entry_id}", str(user_id))
        sanitized_filename = f"{slugify(acquisition.entry.title.lower())}{guess_extension(acquisition.mime)}"

        return FileResponse(acquisition.content, as_attachment=True, filename=sanitized_filename)


class UserAcquisitionDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, user_acquisition_id: uuid.UUID):
        try:
            user_acquisition = UserAcquisition.objects.select_related(
                'acquisition', 'acquisition__entry'
            ).get(pk=user_acquisition_id)
        except UserAcquisition.DoesNotExist:
            raise ProblemDetailException(
                request,
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=ProblemDetailException.DetailType.NOT_FOUND
            )

        if user_acquisition.type == UserAcquisition.UserAcquisitionType.PERSONAL:
            self._authenticate(request)

            if not has_object_permission('check_user_acquisition_read', request.user, user_acquisition):
                raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        redis = Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DATABASE
        )

        redis.pfadd(
            f"evilflowers:popularity:{user_acquisition.acquisition.entry_id}",
            str(uuid.uuid4()) if request.user.is_anonymous else str(request.user.pk)
        )

        sanitized_filename = f"{slugify(user_acquisition.acquisition.entry.title.lower())}" \
                             f"{guess_extension(user_acquisition.acquisition.mime)}"

        if user_acquisition.acquisition.mime in settings.EVILFLOWERS_MODIFIERS:
            modifier = import_string(settings.EVILFLOWERS_MODIFIERS[user_acquisition.acquisition.mime])(
                context={
                    'id': uuid.uuid4() if request.user.is_anonymous else str(user_acquisition.id),
                    'user_id': str(user_acquisition.user_id),
                    'title': user_acquisition.acquisition.entry.title,
                    'username': user_acquisition.user.username,
                    'authors': ', '.join(
                        [
                            user_acquisition.acquisition.entry.author.full_name
                        ] + [
                            c.full_name for c in user_acquisition.acquisition.entry.contributors.all()
                        ]
                    )
                }
            )
            try:

                content = modifier.generate(user_acquisition.acquisition.content, request.GET.get('page', None))
            except IndexError:
                raise ProblemDetailException(request, _("Page not found"), status=HTTPStatus.NOT_FOUND)
        else:
            content = user_acquisition.acquisition.content

        if request.GET.get('format', None) == 'base64':
            return SingleResponse(request, {
                'data': base64.b64encode(content.read()).decode()
            })

        return FileResponse(content, as_attachment=True, filename=sanitized_filename)


class EntryImageDownload(SecuredView):
    UNSECURED_METHODS = ['GET']

    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, image__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(request, _("Entry image not found"), status=HTTPStatus.NOT_FOUND)

        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime)}"

        return FileResponse(entry.image, filename=sanitized_filename)

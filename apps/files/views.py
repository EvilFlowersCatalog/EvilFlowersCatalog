import base64
import uuid
from http import HTTPStatus
from mimetypes import guess_extension

from django.conf import settings
from django.http import FileResponse
from django.urls import reverse
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.response import SingleResponse, SeeOtherResponse
from apps.core.errors import ProblemDetailException, DetailType
from apps.core.fields.multirange import depack
from apps.core.models import Acquisition, Entry, UserAcquisition
from apps.core.modifiers import InvalidPage
from apps.core.views import SecuredView


class AcquisitionDownload(SecuredView):
    UNSECURED_METHODS = ["GET"]

    def get(self, request, acquisition_id: uuid.UUID):
        try:
            acquisition = Acquisition.objects.get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(
                _("Acquisition not found"), status=HTTPStatus.NOT_FOUND
            )

        if acquisition.relation != Acquisition.AcquisitionType.ACQUISITION.OPEN_ACCESS:
            self._authenticate(request)

        if not has_object_permission(
            "check_entry_read", request.user, acquisition.entry
        ):
            raise ProblemDetailException(
                _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
            )

        if (
            request.user.is_authenticated
            and settings.EVILFLOWERS_ENFORCE_USER_ACQUISITIONS
        ):
            if settings.EVILFLOWERS_USER_ACQUISITION_MODE == 'single':
                user_acquisition, created = UserAcquisition.objects.get_or_create(
                    acquisition=acquisition,
                    user=request.user,
                    type=UserAcquisition.UserAcquisitionType.PERSONAL,
                )
            else:
                user_acquisition = UserAcquisition.objects.create(
                    acquisition=acquisition,
                    user=request.user,
                    type=UserAcquisition.UserAcquisitionType.PERSONAL,
                )

            return SeeOtherResponse(
                redirect_to=reverse(
                    "files:user-acquisition-download",
                    kwargs={"user_acquisition_id": user_acquisition.pk},
                )
            )

        acquisition.entry.popularity = acquisition.entry.popularity + 1
        sanitized_filename = f"{slugify(acquisition.entry.title.lower())}{guess_extension(acquisition.mime)}"

        return FileResponse(
            acquisition.content, as_attachment=True, filename=sanitized_filename
        )


class UserAcquisitionDownload(SecuredView):
    UNSECURED_METHODS = ["GET"]

    def get(self, request, user_acquisition_id: uuid.UUID):
        try:
            user_acquisition = UserAcquisition.objects.select_related(
                "acquisition", "acquisition__entry"
            ).get(pk=user_acquisition_id)
        except UserAcquisition.DoesNotExist:
            raise ProblemDetailException(
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if user_acquisition.type == UserAcquisition.UserAcquisitionType.PERSONAL:
            self._authenticate(request)

            if not has_object_permission(
                "check_user_acquisition_read", request.user, user_acquisition
            ):
                raise ProblemDetailException(
                    _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN
                )

        user_acquisition.acquisition.entry.popularity = (
            user_acquisition.acquisition.entry.popularity + 1
        )
        user_acquisition.acquisition.entry.save()

        sanitized_filename = (
            f"{slugify(user_acquisition.acquisition.entry.title.lower())}"
            f"{guess_extension(user_acquisition.acquisition.mime)}"
        )

        if user_acquisition.acquisition.mime in settings.EVILFLOWERS_MODIFIERS:
            modifier = import_string(
                settings.EVILFLOWERS_MODIFIERS[user_acquisition.acquisition.mime]
            )(
                context={
                    "id": str(uuid.uuid4())
                    if request.user.is_anonymous
                    else str(user_acquisition.id),
                    "user_id": str(user_acquisition.user_id),
                    "title": user_acquisition.acquisition.entry.title,
                    "username": user_acquisition.user.username,
                    "authors": ", ".join(
                        [user_acquisition.acquisition.entry.author.full_name]
                        + [
                            c.full_name
                            for c in user_acquisition.acquisition.entry.contributors.all()
                        ]
                    ),
                },
                pages=depack(user_acquisition.range)
                if user_acquisition.range
                else None,
            )
            try:
                content = modifier.generate(
                    user_acquisition.acquisition.content, request.GET.get("page", None)
                )
            except InvalidPage:
                raise ProblemDetailException(
                    _("Page not found"), status=HTTPStatus.NOT_FOUND
                )
        else:
            content = user_acquisition.acquisition.content

        if request.GET.get("format", None) == "base64":
            return SingleResponse(
                request, {"data": base64.b64encode(content.read()).decode()}
            )

        return FileResponse(content, as_attachment=True, filename=sanitized_filename)


class EntryImageDownload(SecuredView):
    UNSECURED_METHODS = ["GET"]

    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, image__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(
                _("Entry image not found"), status=HTTPStatus.NOT_FOUND
            )

        sanitized_filename = (
            f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime)}"
        )

        return FileResponse(entry.image, filename=sanitized_filename)

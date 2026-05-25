import base64
import ipaddress
import uuid
from collections import defaultdict
from http import HTTPStatus
from mimetypes import guess_extension

from django.conf import settings
from django.db.models import F
from django.http import FileResponse
from django.urls import reverse
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import SingleResponse, SeeOtherResponse
from apps.core.errors import ProblemDetailException, DetailType, AuthorizationException
from apps.core.fields.multirange import depack
from apps.core.models import Acquisition, Entry, UserAcquisition, AnnotationItem
from apps.core.modifiers import InvalidPage
from apps.core.views import SecuredView
from apps.files.services import AcquisitionStorageError, AcquisitionStorageService


def _get_client_ip(request) -> str:
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    x_real_ip = request.META.get("HTTP_X_REAL_IP")
    if x_real_ip:
        return x_real_ip.strip()
    return request.META.get("REMOTE_ADDR")


def _check_ip_block(request, entry: Entry):
    if not entry.read_config("evilflowers_ip_block"):
        return

    allowed_ranges = settings.EVILFLOWERS_ALLOWED_IP_RANGES
    if allowed_ranges is None:
        return

    client_ip = ipaddress.ip_address(_get_client_ip(request))
    for cidr in allowed_ranges:
        if client_ip in ipaddress.ip_network(cidr, strict=False):
            return

    raise ProblemDetailException(
        _("Access denied"),
        status=HTTPStatus.FORBIDDEN,
        detail=_("Your IP address is not allowed to access this resource"),
    )


class AcquisitionDownload(SecuredView):
    @openapi.metadata(description="Download Acquisition content", tags=["Files"])
    def get(self, request, acquisition_id: uuid.UUID):
        try:
            acquisition = Acquisition.objects.get(pk=acquisition_id)
        except Acquisition.DoesNotExist:
            raise ProblemDetailException(_("Acquisition not found"), status=HTTPStatus.NOT_FOUND)

        # IP-008 Phase 5: auth + IP block + UserAcquisition bookkeeping
        # run FIRST. Only after the requester is allowed do we ask
        # `AcquisitionStorageService` to dispatch (redirect for
        # EXTERNAL_URL, FileResponse for LOCAL). The old code had a
        # `file_url` redirect path that ran BEFORE the auth checks,
        # which let a UUID-guesser pull Dataverse-backed content from
        # private catalogs.
        if acquisition.relation != Acquisition.AcquisitionType.OPEN_ACCESS:
            request.user = self._authenticate(request)

        if not has_object_permission("check_entry_read", request.user, acquisition.entry):
            raise AuthorizationException(request)

        _check_ip_block(request, acquisition.entry)

        if not AcquisitionStorageService.exists(acquisition):
            raise ProblemDetailException(_("Acquisition file not found"), status=HTTPStatus.NOT_FOUND)

        if request.user.is_authenticated and settings.EVILFLOWERS_ENFORCE_USER_ACQUISITIONS:
            if settings.EVILFLOWERS_USER_ACQUISITION_MODE == "single":
                user_acquisition = UserAcquisition.objects.filter(
                    acquisition=acquisition,
                    user=request.user,
                    type=UserAcquisition.UserAcquisitionType.PERSONAL,
                ).first()

                if not user_acquisition:
                    user_acquisition = UserAcquisition.objects.create(
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

            params = request.GET.copy()

            return SeeOtherResponse(
                redirect_to=reverse(
                    "files:user-acquisition-download",
                    kwargs={"user_acquisition_id": user_acquisition.pk},
                )
                + "?"
                + params.urlencode()
            )

        # Atomic popularity increment (IP-010 M8 pattern).
        Entry.objects.filter(pk=acquisition.entry_id).update(popularity=F("popularity") + 1)

        if request.GET.get("format", None) == "base64":
            if acquisition.storage_backend == Acquisition.StorageBackend.EXTERNAL_URL:
                raise ProblemDetailException(
                    _("base64 format is not supported for externally stored content"),
                    status=HTTPStatus.BAD_REQUEST,
                )
            return SingleResponse(request, data={"data": base64.b64encode(acquisition.content.read()).decode()})

        try:
            return AcquisitionStorageService.download_response(acquisition)
        except AcquisitionStorageError as exc:
            raise ProblemDetailException(_("Acquisition file not found"), status=HTTPStatus.NOT_FOUND) from exc


class UserAcquisitionDownload(SecuredView):
    @openapi.metadata(description="Download UserAcquisition content", tags=["Files"])
    def get(self, request, user_acquisition_id: uuid.UUID):
        try:
            user_acquisition = UserAcquisition.objects.select_related("acquisition", "acquisition__entry").get(
                pk=user_acquisition_id
            )
        except UserAcquisition.DoesNotExist:
            raise ProblemDetailException(
                _("User acquisition not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if user_acquisition.type == UserAcquisition.UserAcquisitionType.PERSONAL:
            if not has_object_permission("check_user_acquisition_read", request.user, user_acquisition):
                raise AuthorizationException(request)

        _check_ip_block(request, user_acquisition.acquisition.entry)

        user_acquisition.acquisition.entry.popularity = user_acquisition.acquisition.entry.popularity + 1
        user_acquisition.acquisition.entry.save()

        sanitized_filename = (
            f"{slugify(user_acquisition.acquisition.entry.title.lower())}"
            f"{guess_extension(user_acquisition.acquisition.mime)}"
        )

        if user_acquisition.acquisition.mime in settings.EVILFLOWERS_MODIFIERS:
            modifier = import_string(settings.EVILFLOWERS_MODIFIERS[user_acquisition.acquisition.mime])(
                context={
                    "id": str(uuid.uuid4()) if request.user.is_anonymous else str(user_acquisition.id),
                    "user_id": str(user_acquisition.user_id),
                    "title": user_acquisition.acquisition.entry.title,
                    "username": user_acquisition.user.username,
                    "authors": ", ".join([a.full_name for a in user_acquisition.acquisition.entry.authors.all()]),
                    "language": (
                        user_acquisition.acquisition.entry.language.alpha2
                        if user_acquisition.acquisition.entry.language
                        else None
                    ),
                },
                pages=depack(user_acquisition.range) if user_acquisition.range else None,
            )

            annotation_map = defaultdict(list)
            if request.GET.get("annotations", None) == "true":
                annotation_items = AnnotationItem.objects.filter(annotation__user_acquisition=user_acquisition).values(
                    "page", "content"
                )
                for item in annotation_items:
                    annotation_map[item["page"]].append(item["content"])
                annotation_map = dict(annotation_map)

            try:
                content = modifier.generate(
                    user_acquisition.acquisition.content,
                    request.GET.get("page", None),
                    annotation_map=dict(annotation_map),
                )
            except InvalidPage:
                raise ProblemDetailException(_("Page not found"), status=HTTPStatus.NOT_FOUND)
        else:
            content = user_acquisition.acquisition.content

        if request.GET.get("format", None) == "base64":
            return SingleResponse(request, data={"data": base64.b64encode(content.read()).decode()})

        return FileResponse(content, as_attachment=True, filename=sanitized_filename)


class EntryImageDownload(SecuredView):
    @openapi.metadata(description="Download Entry cover image", tags=["Files"])
    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, image__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Entry image not found"), status=HTTPStatus.NOT_FOUND)

        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime)}"

        if not entry.image.storage.exists(entry.image.name):
            raise ProblemDetailException(_("Entry image file not found"), status=HTTPStatus.NOT_FOUND)

        return FileResponse(entry.image, filename=sanitized_filename)


class EntryThumbnailDownload(SecuredView):
    @openapi.metadata(description="Download Entry thumbnail", tags=["Files"])
    def get(self, request, entry_id: uuid.UUID):
        try:
            entry = Entry.objects.get(pk=entry_id, thumbnail__isnull=False)
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Entry thumbnail not found"), status=HTTPStatus.NOT_FOUND)

        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime)}"

        if not entry.thumbnail.storage.exists(entry.thumbnail.name):
            raise ProblemDetailException(_("Entry thumbnail file not found"), status=HTTPStatus.NOT_FOUND)

        return FileResponse(streaming_content=entry.thumbnail, filename=sanitized_filename)

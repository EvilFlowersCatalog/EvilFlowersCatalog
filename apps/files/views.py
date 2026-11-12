import base64
import hashlib
import ipaddress
import logging
import uuid
from typing import Optional
from collections import defaultdict
from http import HTTPStatus
from mimetypes import guess_extension

from django.conf import settings
from django.db.models import F
from django.http import FileResponse
from django.urls import reverse
from django.utils.cache import get_conditional_response
from django.utils.http import http_date, quote_etag
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.response import SingleResponse, SeeOtherResponse
from apps.core.errors import ProblemDetailException, DetailType, AuthorizationException
from apps.core.fields.multirange import depack
from apps.core.models import Acquisition, Entry, UserAcquisition, AnnotationItem, UserActivity
from apps.core.modifiers import InvalidPage
from apps.core.services.activity import ActivityService
from apps.core.views import SecuredView
from apps.files import personalised
from apps.files.ranges import is_continuation, ranged_file_response
from apps.files.services import AcquisitionStorageError, AcquisitionStorageService
from evil_flowers_catalog import domain_metrics

logger = logging.getLogger(__name__)


def _stored_size(content) -> int:
    """Bytes of a stored file; 0 when it is missing or empty (#112)."""
    if not content:
        return 0
    try:
        return content.storage.size(content.name)
    except (FileNotFoundError, OSError):
        return 0


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
                # A ranged reader re-requests the same download many times; only its first request opens a copy.
                user_acquisition = None
                if is_continuation(request):
                    user_acquisition = (
                        UserAcquisition.objects.filter(
                            acquisition=acquisition,
                            user=request.user,
                            type=UserAcquisition.UserAcquisitionType.PERSONAL,
                        )
                        .order_by("-created_at")
                        .first()
                    )
                if user_acquisition is None:
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

        if not is_continuation(request):
            # Atomic popularity increment (IP-010 M8 pattern).
            Entry.objects.filter(pk=acquisition.entry_id).update(popularity=F("popularity") + 1)

            ActivityService.record(
                request.user,
                acquisition.entry,
                UserActivity.ActivityAction.ENTRY_DOWNLOADED,
                {"acquisition_id": str(acquisition.pk), "mime": acquisition.mime, "via": "acquisition"},
            )

        if request.GET.get("format", None) == "base64":
            if acquisition.storage_backend == Acquisition.StorageBackend.EXTERNAL_URL:
                raise ProblemDetailException(
                    _("base64 format is not supported for externally stored content"),
                    status=HTTPStatus.BAD_REQUEST,
                )
            response = SingleResponse(request, data={"data": base64.b64encode(acquisition.content.read()).decode()})
            domain_metrics.record_acquisition_served(acquisition.mime, "acquisition", "base64")
            return response

        try:
            response = AcquisitionStorageService.download_response(acquisition, request)
        except AcquisitionStorageError as exc:
            raise ProblemDetailException(_("Acquisition file not found"), status=HTTPStatus.NOT_FOUND) from exc

        response["Cache-Control"] = settings.EVILFLOWERS_FILES_CACHE_CONTROL_PRIVATE
        if not is_continuation(request):
            domain_metrics.record_acquisition_served(acquisition.mime, "acquisition", "file")
        return response


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

        modified = user_acquisition.acquisition.mime in settings.EVILFLOWERS_MODIFIERS
        # A ranged reader re-requests the same download many times; only its first request counts.
        counted = not is_continuation(request)

        if counted:
            user_acquisition.acquisition.entry.popularity = user_acquisition.acquisition.entry.popularity + 1
            user_acquisition.acquisition.entry.save()

            ActivityService.record(
                request.user,
                user_acquisition.acquisition.entry,
                UserActivity.ActivityAction.ENTRY_DOWNLOADED,
                {
                    "acquisition_id": str(user_acquisition.acquisition_id),
                    "mime": user_acquisition.acquisition.mime,
                    "via": "user_acquisition",
                },
            )

        sanitized_filename = (
            f"{slugify(user_acquisition.acquisition.entry.title.lower())}"
            f"{guess_extension(user_acquisition.acquisition.mime)}"
        )

        stored_copy = None
        if modified:
            context = {
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
            }
            modifier = import_string(settings.EVILFLOWERS_MODIFIERS[user_acquisition.acquisition.mime])(
                context=context,
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

            page = request.GET.get("page", None)
            annotation_map = dict(annotation_map)

            def render():
                return modifier.generate(user_acquisition.acquisition.content, page, annotation_map=annotation_map)

            source_storage = user_acquisition.acquisition.content.storage
            try:
                # Rendered once and stored, so every byte range of this download is cut from the same copy (#70).
                # An anonymous copy carries a random id and base64 is never ranged: those are rendered per request.
                if not request.user.is_anonymous and request.GET.get("format", None) != "base64":
                    if _stored_size(user_acquisition.acquisition.content):
                        stored_copy = personalised.copy_name(user_acquisition, context, page, annotation_map)
                        if not personalised.ensure_copy(source_storage, stored_copy, render):
                            stored_copy = None
                content = None if stored_copy else render()
            except InvalidPage:
                raise ProblemDetailException(_("Page not found"), status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                # An empty or damaged stored file (#112): the copy cannot be made, and retrying will not help.
                logger.exception("Could not personalise acquisition %s", user_acquisition.acquisition_id)
                raise ProblemDetailException(
                    _("The file of this publication is damaged or missing."), status=HTTPStatus.GONE
                ) from exc
        else:
            content = user_acquisition.acquisition.content
            if not _stored_size(content):
                raise ProblemDetailException(_("Acquisition file not found"), status=HTTPStatus.NOT_FOUND)

        mime = user_acquisition.acquisition.mime
        if request.GET.get("format", None) == "base64":
            response = SingleResponse(request, data={"data": base64.b64encode(content.read()).decode()})
            domain_metrics.record_acquisition_served(mime, "user_acquisition", "base64")
            return response

        if stored_copy:
            response = ranged_file_response(
                request,
                source_storage,
                stored_copy,
                content_type=mime or None,
                filename=sanitized_filename,
                as_attachment=True,
            )
        elif modified:
            # A copy rendered for this request alone: no Accept-Ranges, the next render would differ.
            response = FileResponse(content, as_attachment=True, filename=sanitized_filename)
        else:
            response = ranged_file_response(
                request,
                content.storage,
                content.name,
                content_type=mime or None,
                filename=sanitized_filename,
                as_attachment=True,
            )
        response["Cache-Control"] = settings.EVILFLOWERS_FILES_CACHE_CONTROL_PRIVATE
        if counted:
            domain_metrics.record_acquisition_served(mime, "user_acquisition", "file")
        return response


def _cover_etag(entry: Entry, file, content_hash: Optional[str]) -> str:
    """Validator for a cover or thumbnail, computed without opening the file.

    The stored content hash (#128) is the strong ETag: only a new file changes it,
    never a save that merely bumps `touched_at` (opening a book saves the entry).
    Rows not yet backfilled fall back to the file name plus `touched_at`.
    """
    if content_hash:
        return quote_etag(content_hash)
    raw = f"{entry.pk}|{file.name}|{entry.touched_at.isoformat() if entry.touched_at else ''}"
    return quote_etag(hashlib.sha256(raw.encode()).hexdigest()[:32])


def _cover_entry(entry_id: uuid.UUID, field: str, missing: str) -> Entry:
    """The entry behind a cover or thumbnail, with only the columns serving it reads (#126).

    A home page asks for ~65 thumbnails at once; the full row carries `content`,
    `table_of_contents` and the search columns.
    """
    try:
        entry = Entry.objects.only(
            "id", "title", "image_mime", "thumbnail_mime", "touched_at", field, f"{field}_hash"
        ).get(pk=entry_id, **{f"{field}__isnull": False})
    except Entry.DoesNotExist:
        raise ProblemDetailException(missing, status=HTTPStatus.NOT_FOUND)
    # An entry without a cover stores "" rather than NULL; serving it opened the storage root (500).
    if not getattr(entry, field):
        raise ProblemDetailException(missing, status=HTTPStatus.NOT_FOUND)
    return entry


def _cover_response(request, entry: Entry, file, content_hash: Optional[str], content_type, filename: str):
    """Serve a cover or thumbnail behind its ETag (IP-027, #128).

    - `?v=` equal to the stored content hash: the URL names this exact body, so
      it is `immutable` for a year (browser and nginx never ask again).
    - no `v` (OPDS clients keep stored URLs) or a stale `v` (the cover was
      replaced): `public, no-cache`, so a cache keeps a copy but revalidates
      it. A stale `v` is never pinned under the immutable policy.

    A matching `If-None-Match` (or `If-Modified-Since` on rows without a hash)
    is answered here with a 304, before storage is touched — not even to check
    that the file exists (#126).
    """
    etag = _cover_etag(entry, file, content_hash)
    # With a content hash the strong ETag is the validator; a `touched_at` date would roll on every save.
    last_modified = None if content_hash or not entry.touched_at else int(entry.touched_at.timestamp())
    if content_hash and request.GET.get("v") == content_hash:
        cache_control = settings.EVILFLOWERS_FILES_CACHE_CONTROL_IMMUTABLE
    else:
        cache_control = settings.EVILFLOWERS_FILES_CACHE_CONTROL_PUBLIC
    response = get_conditional_response(request, etag=etag, last_modified=last_modified)
    if response is None:
        if not file.storage.exists(file.name):
            raise ProblemDetailException(_("Entry image file not found"), status=HTTPStatus.NOT_FOUND)
        response = ranged_file_response(request, file.storage, file.name, content_type=content_type, filename=filename)
    response["ETag"] = etag
    if last_modified is not None:
        response["Last-Modified"] = http_date(last_modified)
    response["Cache-Control"] = cache_control
    return response


class EntryImageDownload(SecuredView):
    # Covers are not access-controlled (cover ACLs are IP-010), so the caller never changes the response:
    # the `?access_token=` the portal appends is not verified (a JWT check and a user SELECT per image, #126),
    # and a page of covers from one campus NAT address is not counted against its anonymous request budget.
    authenticates = False
    rate_limited = False

    @openapi.metadata(description="Download Entry cover image", tags=["Files"])
    def get(self, request, entry_id: uuid.UUID):
        entry = _cover_entry(entry_id, "image", _("Entry image not found"))
        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(entry.image_mime or '') or ''}"
        return _cover_response(request, entry, entry.image, entry.image_hash, entry.image_mime, sanitized_filename)


class EntryThumbnailDownload(SecuredView):
    # See `EntryImageDownload`.
    authenticates = False
    rate_limited = False

    @openapi.metadata(description="Download Entry thumbnail", tags=["Files"])
    def get(self, request, entry_id: uuid.UUID):
        entry = _cover_entry(entry_id, "thumbnail", _("Entry thumbnail not found"))

        # thumbnail_mime is set when the thumbnail is generated (JPEG for most
        # covers); legacy rows predate the field and fall back to image_mime.
        thumbnail_mime = entry.thumbnail_mime or entry.image_mime
        sanitized_filename = f"{slugify(entry.title.lower())}{guess_extension(thumbnail_mime or '') or ''}"
        return _cover_response(
            request, entry, entry.thumbnail, entry.thumbnail_hash, thumbnail_mime, sanitized_filename
        )

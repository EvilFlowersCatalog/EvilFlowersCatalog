"""Signed, single-use upload links: how a file reaches a publication over MCP.

JSON-RPC cannot carry a publication. The STU corpus has PDFs past 80 MB, the
MCP request ceiling is 1 MB (`EVILFLOWERS_MCP_MAX_REQUEST_BYTES`), and a model
cannot base64 a file into its own tool call anyway. So the `create_upload_link`
tool does not take the file — it hands back a URL, and the client POSTs the file
there as `multipart/form-data` with any HTTP client (`curl -F content=@book.pdf`).

The URL is the credential, which is why it is shaped the way it is:

- **Signed** with `SECRET_KEY` (`django.core.signing`), so it cannot be forged
  or pointed at another publication.
- **Short-lived** (`EVILFLOWERS_MCP_UPLOAD_TTL`, 15 minutes by default).
- **Single-use**: its nonce is burned in the cache on first use, so a leaked
  link — it sits in the path, and therefore in access logs — is dead by the time
  anyone reads the log.
- **Scoped** to one publication and one user, and the user's `manage` access is
  re-checked when the file arrives, not only when the link was issued.

No `Authorization` header is needed on the upload itself; the client that holds
the MCP credential is not necessarily the process that has the file.
"""

import logging
import mimetypes
import uuid
from datetime import timedelta
from http import HTTPStatus
from typing import Optional

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from object_checker.base_object_checker import has_object_permission

from apps.api.services.entry import attach_acquisition
from apps.core.models import Acquisition, Entry, User

logger = logging.getLogger("apps.mcp.audit")

SALT = "evilflowers.mcp.upload"
UPLOADABLE_MIMES = (Acquisition.AcquisitionMIME.PDF, Acquisition.AcquisitionMIME.EPUB)
COVER_MIMES = tuple(settings.EVILFLOWERS_IMAGE_MIME)
KINDS = ("file", "cover")
_IMAGE_MAGIC = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG", "image/gif": b"GIF8"}


class UploadRefused(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def issue(request, entry: Entry, user: User, *, relation: Optional[str] = None, kind: str = "file") -> dict:
    """Mint an upload link for `entry` and describe how to use it.

    `kind` is signed into the token, so a link issued for a cover cannot be used
    to attach a publication file, and vice versa.
    """
    ttl = settings.EVILFLOWERS_MCP_UPLOAD_TTL
    token = signing.dumps(
        {"e": str(entry.pk), "u": str(user.pk), "n": uuid.uuid4().hex, "r": relation, "k": kind},
        salt=SALT,
        compress=True,
    )
    url = request.build_absolute_uri(reverse("mcp-upload", kwargs={"token": token}))
    cover = kind == "cover"
    return {
        "kind": kind,
        "upload_url": url,
        "method": "POST",
        "field": "content",
        "accepted_mime_types": list(COVER_MIMES if cover else UPLOADABLE_MIMES),
        "max_bytes": (
            settings.EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE if cover else settings.EVILFLOWERS_MCP_UPLOAD_MAX_BYTES
        ),
        "expires_at": (timezone.now() + timedelta(seconds=ttl)).isoformat(),
        "single_use": True,
        "example": (
            f"curl -sS -F 'content=@\"/path/to/cover.jpg\";type=image/jpeg' '{url}'"
            if cover
            else f"curl -sS -F 'content=@\"/path/to/file.pdf\";type=application/pdf' '{url}'"
        ),
    }


def _redeem(token: str) -> dict:
    try:
        claims = signing.loads(token, salt=SALT, max_age=settings.EVILFLOWERS_MCP_UPLOAD_TTL)
    except signing.SignatureExpired:
        raise UploadRefused(HTTPStatus.GONE, _("This upload link has expired. Ask for a new one."))
    except signing.BadSignature:
        raise UploadRefused(HTTPStatus.NOT_FOUND, _("Unknown upload link."))
    return claims


def _mime(uploaded) -> str:
    """The declared type if it is one we store, else a guess from the file name.

    `curl -F content=@file.pdf` sends `application/octet-stream` unless told
    otherwise, so the name is a legitimate fallback — but only to a type in
    `UPLOADABLE_MIMES`; anything else is refused rather than stored mislabelled.
    """
    declared = (uploaded.content_type or "").split(";")[0].strip().lower()
    if declared in UPLOADABLE_MIMES:
        return declared
    guessed, _encoding = mimetypes.guess_type(uploaded.name or "")
    if guessed in UPLOADABLE_MIMES:
        return guessed
    raise UploadRefused(
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        _("Unsupported file type '%(type)s'. Accepted: %(accepted)s.")
        % {"type": declared or "unknown", "accepted": ", ".join(UPLOADABLE_MIMES)},
    )


def _sniff(uploaded, mime: str) -> None:
    """Refuse a file whose first bytes contradict its type — a mislabelled upload
    would otherwise be encrypted and lent as a book nobody can open."""
    head = uploaded.read(4)
    uploaded.seek(0)
    expected = b"%PDF" if mime == Acquisition.AcquisitionMIME.PDF else b"PK\x03\x04"
    if head != expected:
        raise UploadRefused(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            _("The file does not look like %(type)s.") % {"type": mime},
        )


def _encryption(acquisition: Acquisition) -> Optional[dict]:
    from apps.readium.models import EncryptedContent

    encrypted = EncryptedContent.objects.filter(acquisition=acquisition).first()
    if encrypted is None:
        return None
    return {"content_id": encrypted.lcp_content_id, "status": encrypted.status}


@method_decorator(csrf_exempt, name="dispatch")
class McpUploadEndpoint(View):
    http_method_names = ["post", "options"]

    def post(self, request, token: str):
        try:
            return self._accept(request, token)
        except UploadRefused as refusal:
            return JsonResponse({"error": str(refusal)}, status=refusal.status)

    def _accept(self, request, token: str) -> JsonResponse:
        claims = _redeem(token)

        user = User.objects.filter(pk=claims["u"], is_active=True).first()
        entry = Entry.objects.select_related("catalog").filter(pk=claims["e"]).first()
        if user is None or entry is None:
            raise UploadRefused(HTTPStatus.NOT_FOUND, _("Unknown upload link."))
        # Re-checked at upload time: access revoked after the link was issued
        # must stop the upload, not merely future links.
        if not has_object_permission("check_entry_manage", user, entry):
            raise UploadRefused(HTTPStatus.FORBIDDEN, _("The user who requested this link can no longer manage it."))

        uploaded = request.FILES.get("content")
        if uploaded is None:
            raise UploadRefused(
                HTTPStatus.BAD_REQUEST, _("Send the file as multipart/form-data in a field named `content`.")
            )
        if claims.get("k") == "cover":
            return self._accept_cover(entry, user, uploaded, claims)
        if uploaded.size > settings.EVILFLOWERS_MCP_UPLOAD_MAX_BYTES:
            raise UploadRefused(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                _("File exceeds %(limit)d bytes.") % {"limit": settings.EVILFLOWERS_MCP_UPLOAD_MAX_BYTES},
            )
        mime = _mime(uploaded)
        _sniff(uploaded, mime)

        # Burned only once the request is known to be acceptable, so a typo in
        # the curl command does not cost the caller a fresh link. `cache.add` is
        # atomic, so two concurrent uses of one link cannot both get through.
        if not cache.add(f"mcp:upload:{claims['n']}", "used", timeout=settings.EVILFLOWERS_MCP_UPLOAD_TTL + 60):
            raise UploadRefused(HTTPStatus.GONE, _("This upload link has already been used."))

        acquisition = attach_acquisition(entry, uploaded, mime=mime, relation=claims.get("r"))

        logger.info(
            "mcp.write user=%s action=upload entry=%s acquisition=%s mime=%s bytes=%s",
            user.pk,
            entry.pk,
            acquisition.pk,
            mime,
            uploaded.size,
        )

        return JsonResponse(
            {
                "acquisition_id": str(acquisition.pk),
                "entry_id": str(entry.pk),
                "mime": mime,
                "bytes": uploaded.size,
                "readium_enabled": bool(entry.read_config("readium_enabled")),
                # `null` for an unprotected publication. For a protected one the
                # job starts `pending`; poll `list_encryption_jobs` until it is
                # `registered` — only then can the publication be lent.
                "encryption": _encryption(acquisition),
            },
            status=HTTPStatus.CREATED,
        )

    @staticmethod
    def _accept_cover(entry: Entry, user: User, uploaded, claims: dict) -> JsonResponse:
        if uploaded.size > settings.EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE:
            raise UploadRefused(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                _("Cover exceeds %(limit)d bytes.") % {"limit": settings.EVILFLOWERS_IMAGE_UPLOAD_MAX_SIZE},
            )
        declared = (uploaded.content_type or "").split(";")[0].strip().lower()
        mime = declared if declared in COVER_MIMES else (mimetypes.guess_type(uploaded.name or "")[0] or "")
        if mime not in COVER_MIMES:
            raise UploadRefused(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                _("Unsupported cover type '%(type)s'. Accepted: %(accepted)s.")
                % {"type": declared or "unknown", "accepted": ", ".join(COVER_MIMES)},
            )
        head = uploaded.read(4)
        uploaded.seek(0)
        if not head.startswith(_IMAGE_MAGIC[mime]):
            raise UploadRefused(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, _("The file does not look like %(type)s.") % {"type": mime}
            )
        try:
            from PIL import Image

            Image.open(uploaded).verify()
        except Exception:
            raise UploadRefused(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, _("The cover image could not be decoded."))
        uploaded.seek(0)

        if not cache.add(f"mcp:upload:{claims['n']}", "used", timeout=settings.EVILFLOWERS_MCP_UPLOAD_TTL + 60):
            raise UploadRefused(HTTPStatus.GONE, _("This upload link has already been used."))

        from apps.api.services.entry import set_cover

        old = (entry.image.name, entry.thumbnail.name)
        set_cover(entry, uploaded, mime=mime)  # FieldFile.save() persists the entry, as the REST path does
        # A replaced cover can land on a new path (e.g. cover.png → cover.jpg).
        for previous, current in zip(old, (entry.image.name, entry.thumbnail.name)):
            if previous and previous != current:
                entry.image.storage.delete(previous)

        logger.info(
            "mcp.write user=%s action=upload_cover entry=%s mime=%s bytes=%s", user.pk, entry.pk, mime, uploaded.size
        )
        return JsonResponse(
            {
                "entry_id": str(entry.pk),
                "kind": "cover",
                "mime": mime,
                "bytes": uploaded.size,
                "thumbnail_mime": entry.thumbnail_mime,
            },
            status=HTTPStatus.CREATED,
        )


__all__ = ["COVER_MIMES", "KINDS", "McpUploadEndpoint", "UPLOADABLE_MIMES", "issue"]

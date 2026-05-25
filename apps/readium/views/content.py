"""
Encrypted Content Download View

Serves LCP-encrypted publication files to reading apps.
No authentication required — the content is encrypted and
only usable with a valid LCP license.
"""

import logging
from http import HTTPStatus

from django.http import FileResponse
from django.utils.translation import gettext as _
from django.views import View

from apps.core.errors import ProblemDetailException, DetailType
from apps.files.storage import get_storage
from apps.readium.models import EncryptedContent

logger = logging.getLogger(__name__)


class EncryptedContentDownloadView(View):
    """
    Serves encrypted content files by LCP content ID.

    URL registered with LCP server: {READIUM_BASE_URL}/readium/v1/content/{lcp_content_id}
    Reading apps download encrypted publications from this endpoint.
    """

    def get(self, request, lcp_content_id: str):
        try:
            encrypted_content = EncryptedContent.objects.select_related("acquisition").get(
                lcp_content_id=lcp_content_id
            )
        except EncryptedContent.DoesNotExist:
            raise ProblemDetailException(
                _("Encrypted content not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        if encrypted_content.status not in (
            EncryptedContent.EncryptionStatus.COMPLETED,
            EncryptedContent.EncryptionStatus.REGISTERED,
        ):
            raise ProblemDetailException(
                _("Encrypted content not ready"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        storage = get_storage()
        if not storage.exists(encrypted_content.encrypted_path):
            logger.error(f"Encrypted file not found on storage: {encrypted_content.encrypted_path}")
            raise ProblemDetailException(
                _("Encrypted file not found"),
                status=HTTPStatus.NOT_FOUND,
                detail_type=DetailType.NOT_FOUND,
            )

        # IP-008 Phase 2 B1: advertise the LCP-protected MIME so reader
        # apps know the payload is encrypted. EPUB stays as-is per the
        # Readium LCP for EPUB profile (LCP-ness lives inside the zip);
        # PDF gets the +lcp variant.
        response = FileResponse(
            storage.open(encrypted_content.encrypted_path),
            content_type=_lcp_content_type(encrypted_content.acquisition.mime),
        )
        # Encrypted content must never be cached by intermediaries.
        response["Cache-Control"] = "private, no-store"
        return response


def _lcp_content_type(source_mime: str) -> str:
    """Map a plain acquisition MIME to its LCP-protected equivalent.

    Per LCP for PDF profile §3, encrypted PDFs are served as
    `application/pdf+lcp`. EPUBs stay as `application/epub+zip` — the
    Readium LCP for EPUB profile specifies that the LCP signal lives
    inside the package, not at the transport MIME.
    """
    return {
        "application/pdf": "application/pdf+lcp",
    }.get(source_mime, source_mime)

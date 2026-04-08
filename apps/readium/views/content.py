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

        content_type = encrypted_content.acquisition.mime
        return FileResponse(
            storage.open(encrypted_content.encrypted_path),
            content_type=content_type,
        )
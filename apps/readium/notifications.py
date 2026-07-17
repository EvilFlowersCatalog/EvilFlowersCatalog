"""
Readium's notification attachment resolvers.

Readium knows how to turn a license into a downloadable `.lcpl`; the
notifications app does not, and must not. This module implements the
`AttachmentResolver` protocol for that one concern and registers it under a
stable name, so a notification can carry `lcpl_attachment_ref(license_id)`
without the notifications layer importing anything readium-specific.

Emailing the `.lcpl` is LCP-compliant: the file is inert without the user's
passphrase, which never travels in it (only the hint does). See the EDRLab
certification notebook — its own submission flow emails `.lcpl` files.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from apps.notifications.attachments import AttachmentRef, EmailAttachment

logger = logging.getLogger(__name__)

LCPL_RESOLVER = "readium.lcpl"


def lcpl_attachment_ref(license_id: str) -> AttachmentRef:
    """Build the serialisable ref that yields a license's `.lcpl` at send time."""
    return {"resolver": LCPL_RESOLVER, "params": {"license_id": str(license_id)}}


def resolve_lcpl_attachment(params: dict) -> Optional[EmailAttachment]:
    """Fetch the fresh `.lcpl` for `params["license_id"]` as an attachment.

    Returns None (email still sends without the file) when the license is gone
    or the LCP server is unreachable.
    """
    from apps.readium.models import License
    from apps.readium.services import LicenseService

    license_obj = License.objects.select_related("entry").filter(pk=params.get("license_id")).first()
    if license_obj is None:
        return None

    fresh = LicenseService.fetch_fresh_license(license_obj)
    return EmailAttachment(
        filename=f"{license_obj.entry.title}.lcpl",
        content=json.dumps(fresh),
        mimetype="application/vnd.readium.lcp.license.v1.0+json",
    )

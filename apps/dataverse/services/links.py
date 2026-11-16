"""Links between Elvira and Dataverse: the portal URL a dataset points at (`elviraUrl`, `relatedMaterial`, the
"Open in Elvira" external tool via `/resolve/dataverse`)."""

from typing import Optional

from django.conf import settings


def portal_entry_url(entry_id) -> Optional[str]:
    """The portal's entry detail: `content.cite_base_url`, or `EVILFLOWERS_PORTAL_URL` when that is empty.
    None when neither is configured."""
    from apps.content.citation import entry_url

    url = entry_url(entry_id)
    if url.startswith("/"):
        base = (getattr(settings, "EVILFLOWERS_PORTAL_URL", "") or "").rstrip("/")
        if not base:
            return None
        url = f"{base}{url}"
    return url


__all__ = ["portal_entry_url"]

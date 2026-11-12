"""Notification branding: what the e-mails look like for this deployment (IP-025).

Three layers, most specific first:

1. **Template overrides on a volume** — `EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR`,
   mounted by IaC from the knowledge repository. `templates/` replaces whole
   templates (Django finds them before the built-in ones); `assets/` holds
   images the catalog serves publicly (the STU logo).
2. **Branding tokens in the database** — `NotificationBranding`, edited by a
   superuser over MCP (`update_notification_branding`): name, logo, colours,
   footer. The built-in `base.mjml` consumes them, and so does any override
   that keeps using `{{ brand.* }}`.
3. **Settings and built-in defaults** — what an untouched deployment sends.

Tokens are resolved once per send, straight from the database.
"""

import logging
import os
import re
from dataclasses import asdict, dataclass, replace
from typing import List, Optional

from django.conf import settings
from django.urls import reverse

logger = logging.getLogger(__name__)

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: What mail clients render. SVG is refused: Gmail and Outlook drop it.
ASSET_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}

DEFAULTS = {
    "header_background": "#4A90D9",
    "header_text_color": "#ffffff",
    "primary_color": "#4A90D9",
    "button_text_color": "#ffffff",
    "background_color": "#f4f4f4",
    "logo_width": 200,
}


@dataclass(frozen=True)
class Branding:
    library_name: str
    logo: str = ""
    logo_alt: str = ""
    logo_width: int = DEFAULTS["logo_width"]
    logo_link: str = ""
    header_background: str = DEFAULTS["header_background"]
    header_text_color: str = DEFAULTS["header_text_color"]
    primary_color: str = DEFAULTS["primary_color"]
    button_text_color: str = DEFAULTS["button_text_color"]
    background_color: str = DEFAULTS["background_color"]
    footer_text: str = ""
    website_url: str = ""
    from_name: str = ""
    #: Absolute URL a mail client can fetch without credentials; "" when there is no usable logo.
    logo_url: str = ""

    def as_context(self) -> dict:
        data = asdict(self)
        data["logo_alt"] = self.logo_alt or self.library_name
        data["logo_link"] = self.logo_link or self.website_url
        return data


def overrides_dir() -> str:
    return getattr(settings, "EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR", "") or ""


def assets_dir() -> str:
    base = overrides_dir()
    return os.path.join(base, "assets") if base else ""


def asset_path(name: str) -> Optional[str]:
    """Filesystem path of a servable asset, or None. Rejects anything that could leave the directory."""
    directory = assets_dir()
    if not directory or not ASSET_NAME.match(name or ""):
        return None
    if os.path.splitext(name)[1].lower() not in ASSET_TYPES:
        return None
    path = os.path.join(directory, name)
    return path if os.path.isfile(path) else None


def list_assets() -> List[str]:
    directory = assets_dir()
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory) if asset_path(name))


def list_template_overrides() -> List[str]:
    """Template files the volume overrides, relative to `templates/notifications/`."""
    base = overrides_dir()
    root = os.path.join(base, "templates", "notifications") if base else ""
    if not root or not os.path.isdir(root):
        return []
    found = []
    for directory, _subdirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def is_absolute_logo(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def logo_url(logo: str) -> str:
    if not logo:
        return ""
    if is_absolute_logo(logo):
        return logo
    if asset_path(logo) is None:
        logger.warning("Notification logo asset %r is not in %s; sending the text header", logo, assets_dir())
        return ""
    base = settings.EVILFLOWERS_BASE_URL.rstrip("/")
    return f"{base}{reverse('notification-asset', kwargs={'name': logo})}"


def defaults() -> Branding:
    """What the e-mails look like with no database row (settings + built-in defaults)."""
    return Branding(library_name=settings.EVILFLOWERS_NOTIFICATION_LIBRARY_NAME)


def build(row) -> Branding:
    branding = defaults()
    if row is not None:
        from apps.notifications.models import NotificationBranding

        stored = {
            name: getattr(row, name)
            for name in NotificationBranding.TOKEN_FIELDS
            if getattr(row, name) not in (None, "")
        }
        branding = replace(branding, **stored)
    return replace(branding, logo_url=logo_url(branding.logo))


def resolve() -> Branding:
    """The branding for the next e-mail. One indexed query per send — notifications are
    low-volume, and an uncached read means an edit applies to the very next e-mail."""
    from apps.notifications.models import NotificationBranding

    return build(NotificationBranding.current())


def validate_color(value: str) -> str:
    if not HEX_COLOR.match(value):
        raise ValueError(f"'{value}' is not a hex colour like #a60c2f")
    return value.lower()


def validate_logo(value: str) -> str:
    if is_absolute_logo(value):
        if not value.startswith("https://"):
            raise ValueError("A logo URL must use https — mail clients block mixed content.")
        if value.lower().split("?")[0].endswith(".svg"):
            raise ValueError("SVG logos do not render in Gmail or Outlook; use PNG or JPEG.")
        return value
    if asset_path(value) is None:
        available = (
            ", ".join(list_assets()) or "none — EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR/assets is empty or unset"
        )
        raise ValueError(f"'{value}' is not an asset on the overrides volume. Available: {available}.")
    return value


__all__ = [
    "ASSET_TYPES",
    "Branding",
    "DEFAULTS",
    "asset_path",
    "build",
    "defaults",
    "list_assets",
    "list_template_overrides",
    "resolve",
    "validate_color",
    "validate_logo",
]

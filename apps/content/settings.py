"""The `content.*` instance settings (#20, #174), registered when the app is ready."""

from django.conf import settings

from apps.core import settings_store
from apps.core.settings_store import SettingDefinition, SettingError


def _policy_exists(value: str) -> None:
    from apps.content import policies

    if not policies.exists(value):
        known = ", ".join(policy.key for policy in policies.all_current())
        raise SettingError(f"content.default_policy: no content policy '{value}'. Known: {known}.")


def _portal_url() -> str:
    return (getattr(settings, "EVILFLOWERS_PORTAL_URL", "") or settings.EVILFLOWERS_BASE_URL).rstrip("/")


settings_store.register(
    SettingDefinition(
        key="content.default_policy",
        schema={"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
        description="The content policy of every entry whose catalog and entry assign none (#174).",
        default="parity",
        validate=_policy_exists,
        tags=["content"],
    )
)

settings_store.register(
    SettingDefinition(
        key="content.cite_base_url",
        schema={"type": "string", "pattern": "^https?://", "maxLength": 500},
        description="Base URL of citation links (`/cite/...`) handed to agents and readers.",
        default=_portal_url,
        fallback="EVILFLOWERS_CITE_BASE_URL",
        tags=["content"],
    )
)

settings_store.register(
    SettingDefinition(
        key="content.external_processor_default",
        schema={"type": "string", "enum": ["local", "external"]},
        description="The processor of an API key that declares none: where its client sends the text it reads.",
        default="external",
        tags=["content"],
    )
)

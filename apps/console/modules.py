"""IP-035: the functional modules of the administration console, and which of them are on.

A module is a group of models that one kind of work needs (lending, the extraction pipeline, ...),
across Django apps. Each module is switched in three layers, the first that says "off" wins:

1. `requires`: the feature flag of the code behind it (`EVILFLOWERS_ASSISTANT_ENABLED`, ...). A module
   for a feature the deployment turned off is never shown.
2. the instance setting `admin.modules` (`apps.core.settings_store`), changed at runtime on the
   console's Modules page, over the REST settings API or the MCP `update_setting` tool;
3. the deployment default `EVILFLOWERS_ADMIN_MODULES` (comma-separated keys, `all`, or unset for each
   module's own `default`), which the setting above inherits.

Sensitive modules (readers' activity, chat transcripts, disclosure logs) are off by default: switching
one on is a decision someone makes and the settings row records who made it.

Every concrete model of the project must either belong to a module or be listed in `EXCLUDED_MODELS`
with the reason (`apps.console.tests` enforces it), so "the admin covers the whole database" stays true
when someone adds a model.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from django.conf import settings
from django.utils.translation import gettext_lazy as _

SETTING_KEY = "admin.modules"


@dataclass(frozen=True)
class Module:
    key: str
    title: str
    description: str
    #: Shown when no setting says otherwise.
    default: bool = True
    #: Holds personal data beyond account basics (reading behaviour, conversations, disclosures).
    sensitive: bool = False
    #: Name of a boolean Django setting the module needs (the feature flag of the code behind it).
    requires: Optional[str] = None
    #: A material symbol-like glyph name used by the templates (a small inline SVG set, see `console/icons.html`).
    icon: str = "grid"

    def available(self) -> bool:
        return self.requires is None or bool(getattr(settings, self.requires, False))


MODULES: List[Module] = [
    Module(
        "library",
        _("Library"),
        _("Catalogs, publications, authors, categories, feeds, files and reviews."),
        icon="book",
    ),
    Module(
        "accounts",
        _("People & access"),
        _("Users, groups, authentication sources, API keys and catalog memberships."),
        icon="users",
    ),
    Module(
        "lending",
        _("Lending"),
        _("Readium LCP: encrypted publications, licenses, the reservation queue and user acquisitions."),
        icon="key",
    ),
    Module(
        "jobs",
        _("Background jobs"),
        _("The jobs ledger (IP-031): desired and observed state, runs, worker events."),
        icon="cog",
    ),
    Module(
        "extraction",
        _("Extraction pipeline"),
        _("Runs, regions, paragraphs, sentences, locators, links and bibliographies (IP-018)."),
        requires="EVILFLOWERS_EXTRACTION_ENABLED",
        icon="layers",
    ),
    Module(
        "knowledge",
        _("Knowledge & decisions"),
        _("Subject suggestions, typed section relations, mentions and figure links, the knowledge graph."),
        icon="share",
    ),
    Module(
        "content",
        _("Content access"),
        _("Content policies, per-reader grants and evidence packs (#171–#176)."),
        icon="shield",
    ),
    Module(
        "assistant",
        _("Assistant"),
        _("Section summaries and per-user assistant limits."),
        requires="EVILFLOWERS_ASSISTANT_ENABLED",
        icon="spark",
    ),
    Module(
        "interop",
        _("Interoperability"),
        _("OAI-PMH records, harvest sources, records, conflicts and match suggestions (IP-033/034)."),
        icon="swap",
    ),
    Module(
        "notifications",
        _("Notifications"),
        _("E-mail branding, contacts and the delivery log."),
        icon="mail",
    ),
    Module(
        "system",
        _("System"),
        _("Instance settings, reference data (languages, currencies) and the admin audit log."),
        icon="sliders",
    ),
    Module(
        "reading",
        _("Reading activity"),
        _("Readers' shelves, annotations, activity history and read events."),
        default=False,
        sensitive=True,
        icon="eye",
    ),
    Module(
        "conversations",
        _("Assistant conversations"),
        _("Readers' chats with the assistant, message by message."),
        default=False,
        sensitive=True,
        requires="EVILFLOWERS_ASSISTANT_ENABLED",
        icon="chat",
    ),
    Module(
        "audit",
        _("Disclosure audit"),
        _("Which text of which publication was disclosed to whom, through which surface (#175)."),
        default=False,
        sensitive=True,
        icon="list",
    ),
]

BY_KEY: Dict[str, Module] = {module.key: module for module in MODULES}

#: Models deliberately left out of the console, with the reason. `app_label.ModelName`.
EXCLUDED_MODELS: Dict[str, str] = {
    "sessions.Session": "Session payloads are bearer secrets; nothing to administer by hand.",
    "auth.Permission": "Framework rows, edited through a group's or user's permission widget.",
    "contenttypes.ContentType": "Framework rows, created by migrations.",
}


def module(key: str) -> Module:
    return BY_KEY[key]


def deployment_defaults() -> Dict[str, bool]:
    """What `EVILFLOWERS_ADMIN_MODULES` turns on; unset keeps every module's own default."""
    raw = getattr(settings, "EVILFLOWERS_ADMIN_MODULES", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {item.key: item.default for item in MODULES}
    if isinstance(raw, str):
        keys = {part.strip() for part in raw.split(",") if part.strip()}
    else:
        keys = set(raw)
    if "all" in keys:
        return {item.key: True for item in MODULES}
    return {item.key: item.key in keys for item in MODULES}


def _stored_overrides() -> Dict[str, bool]:
    from apps.core import settings_store

    try:
        value = settings_store.get(SETTING_KEY)
    except Exception:  # pragma: no cover - a cache or database outage must not take the admin down
        return {}
    return value if isinstance(value, dict) else {}


def states() -> Dict[str, dict]:
    """Per module: `enabled` (the effective answer), `available` (feature flag), `source` of the switch."""
    defaults = deployment_defaults()
    overrides = _stored_overrides()
    result = {}
    for item in MODULES:
        if item.key in overrides:
            wanted, source = bool(overrides[item.key]), "setting"
        else:
            wanted, source = defaults[item.key], "deployment"
        available = item.available()
        result[item.key] = {
            "module": item,
            "available": available,
            "wanted": wanted,
            "enabled": available and wanted,
            "source": source if available else "feature flag",
        }
    return result


def is_enabled(key: str) -> bool:
    item = BY_KEY[key]
    if not item.available():
        return False
    overrides = _stored_overrides()
    if key in overrides:
        return bool(overrides[key])
    return deployment_defaults()[key]


def _validate(value) -> None:
    from apps.core.settings_store import SettingError

    unknown = sorted(set(value) - set(BY_KEY))
    if unknown:
        raise SettingError(f"{SETTING_KEY}: unknown modules {', '.join(unknown)}. Known: {', '.join(BY_KEY)}.")


def register_setting() -> None:
    """Declare `admin.modules` in the instance settings registry (called from `ConsoleConfig.ready`)."""
    from apps.core import settings_store

    settings_store.register(
        settings_store.SettingDefinition(
            key=SETTING_KEY,
            schema={"type": "object", "additionalProperties": {"type": "boolean"}},
            description=(
                "Administration console modules switched on (true) or off (false), by key. A key not "
                "listed follows the deployment default EVILFLOWERS_ADMIN_MODULES. Modules whose feature "
                "flag is off stay hidden whatever this says."
            ),
            default=dict,
            validate=_validate,
            tags=["admin"],
        )
    )

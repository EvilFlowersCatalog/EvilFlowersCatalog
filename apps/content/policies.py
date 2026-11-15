"""Content policies (#174): every content-access rule as a key in a versioned document.

A policy answers, for one publication: which tier of its content each *basis* of
access reaches (`tiers`), which circumstances lower that (`caps`), which facts count
as a loan (`grants`), how much a past borrower may read (`budget`, enforced by #175)
and how long disclosures are logged (`log`). The gate (`apps.content.access`) reads
it; nothing in code hard-wires a rule any more.

A superuser edits policies; a save writes the next version, effective at once.
Catalogs and entries pick a policy by key; resolution goes entry → catalog →
instance setting `content.default_policy` → `parity`, the built-in policy that is
today's behaviour. A key that names no policy is skipped (and logged), so a deleted
or mistyped assignment falls back instead of failing reads.
"""

import copy
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max

from apps.core import settings_store

logger = logging.getLogger(__name__)

#: Ordered from least to most; the gate compares positions.
TIERS = ("metadata", "outline", "snippet", "passage", "full")
BASES = ("open", "loan", "past_loan", "none")
#: `cancelled` is not offered: a loan is cancelled when it is returned before any reading app opened it, so it
#: never grants (#175).
LICENSE_STATES = ("ready", "active", "returned", "expired", "revoked")
ACQUISITION_TYPES = ("personal", "shared")
PROCESSORS = ("local", "external")

PARITY = "parity"
KEY_PATTERN = "^[a-z0-9][a-z0-9-]{0,63}$"
#: Not a policy: the assignment endpoints read it as "clear, inherit from the level above".
RESERVED_KEYS = ("inherit", "schema", "explain")

CACHE_KEY = "content-policies:v1"
CACHE_TIMEOUT = 60 * 60

_TIER = {"type": "string", "enum": list(TIERS)}

#: The JSON Schema every policy document must match. Served at `/api/v1/content-policies/schema`, so the portal
#: renders its form from it.
SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Content policy",
    "type": "object",
    "additionalProperties": False,
    "required": ["key", "title", "tiers", "caps", "grants", "budget", "log"],
    "properties": {
        "key": {"type": "string", "pattern": KEY_PATTERN, "description": "Referenced as `policy:<key>`."},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "description": {"type": "string", "maxLength": 2000},
        "tiers": {
            "type": "object",
            "description": "The highest tier each basis of access reaches.",
            "additionalProperties": False,
            "required": list(BASES),
            "properties": {
                "open": {**_TIER, "description": "Not lent through Readium LCP: anyone who sees the entry."},
                "loan": {**_TIER, "description": "A live licence in `grants.license_states`, or an own copy."},
                "past_loan": {**_TIER, "description": "An ended licence in `grants.license_states`."},
                "none": {**_TIER, "description": "Sees the entry, holds nothing."},
            },
        },
        "caps": {
            "type": "object",
            "description": "Each cap can only lower the tier.",
            "additionalProperties": False,
            "required": ["outside_ip", "processor", "tdm_reserved", "generated"],
            "properties": {
                "outside_ip": {**_TIER, "description": "Reader outside the entry's allowed IP ranges."},
                "processor": {
                    "type": "object",
                    "description": "Where the caller sends the text: our hardware, or a third-party model provider.",
                    "additionalProperties": False,
                    "required": list(PROCESSORS),
                    "properties": {name: _TIER for name in PROCESSORS},
                },
                "tdm_reserved": {**_TIER, "description": "The rights holder reserved text and data mining."},
                "generated": {**_TIER, "description": "Model-written text about the book: summaries, descriptions."},
            },
        },
        "grants": {
            "type": "object",
            "description": "Which facts about a reader count as a loan or a past loan.",
            "additionalProperties": False,
            "required": [
                "license_states",
                "expired_if_opened",
                "acquisition_types",
                "survive_hidden",
                "member_counts_as_inside_ip",
            ],
            "properties": {
                "license_states": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(LICENSE_STATES)},
                    "description": "Readium LCP licence states that grant: `ready`/`active` as a loan, the others "
                    "as a past loan. Empty: an LCP licence never unlocks text.",
                },
                "expired_if_opened": {
                    "type": "boolean",
                    "description": "An expired licence grants a past loan if the reader ever opened it.",
                },
                "acquisition_types": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": list(ACQUISITION_TYPES)},
                    "description": "The reader's own copies that count as a loan (entries without LCP).",
                },
                "survive_hidden": {
                    "type": "boolean",
                    "description": "A grant still applies after the entry is hidden from the reader.",
                },
                "member_counts_as_inside_ip": {
                    "type": "boolean",
                    "description": "A member of the entry's catalog is treated as inside its IP ranges.",
                },
                "regions_on_live_license": {
                    "type": "boolean",
                    "default": True,
                    "description": "A live Readium LCP licence (`ready`/`active`) shows the borrower the figures' "
                    "and tables' captions and crops (a snippet), whatever `license_states` says about text "
                    "(#159). Optional; missing means true.",
                },
            },
        },
        "budget": {
            "description": "How much past borrowers may read (#175); null for no budget.",
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["bases", "percent_of_book", "window_days"],
                    "properties": {
                        "bases": {"type": "array", "uniqueItems": True, "items": {"enum": list(BASES)}},
                        "percent_of_book": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                        "window_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                    },
                },
            ],
        },
        "log": {
            "type": "object",
            "additionalProperties": False,
            "required": ["retention_days"],
            "properties": {"retention_days": {"type": "integer", "minimum": 0, "maximum": 3650}},
        },
    },
}


def _document(key, title, description, tiers, caps, grants, budget=None, retention_days=365) -> dict:
    return {
        "key": key,
        "title": title,
        "description": description,
        "tiers": dict(zip(BASES, tiers)),
        "caps": caps,
        "grants": grants,
        "budget": budget,
        "log": {"retention_days": retention_days},
    }


_LICENSED_STANDARD_CAPS = {
    "outside_ip": "outline",
    "processor": {"local": "full", "external": "snippet"},
    "tdm_reserved": "snippet",
    "generated": "passage",
}
_LICENSED_STANDARD_GRANTS = {
    "license_states": ["active", "returned"],
    "expired_if_opened": True,
    "acquisition_types": ["personal"],
    "survive_hidden": False,
    "member_counts_as_inside_ip": True,
}

#: The five presets the data migration creates (version 1 each). `parity` is also the built-in fallback.
PRESETS: Dict[str, dict] = {
    PARITY: _document(
        PARITY,
        "Today's rules",
        "Open-access text for anyone who sees the entry, inside its IP ranges; Readium LCP text never reaches "
        "a model, loan or not. The instance default and the built-in fallback.",
        ("full", "full", "metadata", "metadata"),
        {
            "outside_ip": "metadata",
            "processor": {"local": "full", "external": "full"},
            "tdm_reserved": "full",
            "generated": "full",
        },
        {
            # No licence state grants: an LCP loan stays at `none` (IP-018/IP-030).
            "license_states": [],
            "expired_if_opened": False,
            "acquisition_types": ["personal"],
            "survive_hidden": False,
            "member_counts_as_inside_ip": False,
        },
    ),
    "open-access": _document(
        "open-access",
        "Open access (CC, public domain)",
        "Everything, to everyone who sees the entry, wherever they are and whatever model they use.",
        ("full", "full", "full", "full"),
        {
            "outside_ip": "full",
            "processor": {"local": "full", "external": "full"},
            "tdm_reserved": "snippet",
            "generated": "full",
        },
        {
            "license_states": ["ready", "active", "returned", "expired"],
            "expired_if_opened": False,
            "acquisition_types": ["personal", "shared"],
            "survive_hidden": False,
            "member_counts_as_inside_ip": True,
        },
    ),
    "licensed-standard": _document(
        "licensed-standard",
        "Licensed titles, borrowers keep passages",
        "For licence terms that allow AI reading by borrowers: a live loan reads everything, a past loan "
        "passages within a budget.",
        ("full", "full", "passage", "metadata"),
        _LICENSED_STANDARD_CAPS,
        _LICENSED_STANDARD_GRANTS,
        budget={"bases": ["past_loan"], "percent_of_book": 10, "window_days": 30},
    ),
    "licensed-strict": _document(
        "licensed-strict",
        "Licensed titles, reader app only",
        "For licence terms that forbid AI use beyond the reader app: a live loan gives snippets, nothing to an "
        "external model.",
        ("full", "snippet", "metadata", "metadata"),
        {
            "outside_ip": "outline",
            "processor": {"local": "full", "external": "metadata"},
            "tdm_reserved": "snippet",
            "generated": "passage",
        },
        {
            "license_states": ["active"],
            "expired_if_opened": False,
            "acquisition_types": ["personal"],
            "survive_hidden": False,
            "member_counts_as_inside_ip": True,
        },
    ),
    "campus-only": _document(
        "campus-only",
        "Campus only",
        "As licensed-standard, but nothing beyond the catalogue record outside the allowed IP ranges, for "
        "members too.",
        ("full", "full", "passage", "metadata"),
        {**_LICENSED_STANDARD_CAPS, "outside_ip": "metadata"},
        {**_LICENSED_STANDARD_GRANTS, "member_counts_as_inside_ip": False},
        budget={"bases": ["past_loan"], "percent_of_book": 10, "window_days": 30},
    ),
}


@dataclass(frozen=True)
class Policy:
    key: str
    version: int
    title: str
    document: dict

    @property
    def ref(self) -> str:
        """`key@version`, as receipts and the log pin it."""
        return f"{self.key}@{self.version}"

    def tier(self, basis: str) -> str:
        return self.document["tiers"][basis]

    @property
    def caps(self) -> dict:
        return self.document["caps"]

    @property
    def grants(self) -> dict:
        return self.document["grants"]

    @property
    def budget(self) -> Optional[dict]:
        return self.document.get("budget")


#: Used when the database has no `parity` row (before the data migration, or after someone deleted it).
BUILTIN_PARITY = Policy(PARITY, 0, PRESETS[PARITY]["title"], PRESETS[PARITY])


class PolicyError(settings_store.SettingError):
    """A document the schema refuses, or a key that is not a policy. Carries the schema `path`."""


def validate(key: str, document: Any) -> dict:
    try:
        settings_store.schema_errors(SCHEMA, document)
    except settings_store.SettingError as error:
        raise PolicyError(error.message, path=error.path, schema_path=error.schema_path) from error
    if key in RESERVED_KEYS:
        raise PolicyError(f"'{key}' is reserved and cannot name a policy.", path="key")
    if document["key"] != key:
        raise PolicyError(f"The document's key '{document['key']}' does not match the policy '{key}'.", path="key")
    return document


# --- store ----------------------------------------------------------------------------------------------------


def _current_map() -> Dict[str, Policy]:
    """The highest version of every policy, from the cache or with one query."""
    cache_key = settings_store.database_cache_key(CACHE_KEY)
    cached = cache.get(cache_key)
    if cached is None:
        from apps.content.models import ContentPolicy

        cached = {}
        for row in ContentPolicy.objects.order_by("key", "-version").distinct("key"):
            cached[row.key] = {"version": row.version, "title": row.title, "document": row.document}
        cache.set(cache_key, cached, CACHE_TIMEOUT)
    return {key: Policy(key, value["version"], value["title"], value["document"]) for key, value in cached.items()}


def invalidate() -> None:
    cache.delete(settings_store.database_cache_key(CACHE_KEY))


def current(key: str) -> Optional[Policy]:
    policy = _current_map().get(key)
    if policy is None and key == PARITY:
        return BUILTIN_PARITY
    return policy


def all_current() -> List[Policy]:
    policies = _current_map()
    policies.setdefault(PARITY, BUILTIN_PARITY)
    return [policies[key] for key in sorted(policies)]


def exists(key: str) -> bool:
    return current(key) is not None


def require(key: str) -> Policy:
    policy = current(key)
    if policy is None:
        known = ", ".join(policy.key for policy in all_current())
        raise PolicyError(f"No content policy '{key}'. Known: {known}.", path="key")
    return policy


def version(key: str, number: int) -> Optional[Policy]:
    from apps.content.models import ContentPolicy

    row = ContentPolicy.objects.filter(key=key, version=number).first()
    if row is None:
        return BUILTIN_PARITY if (key, number) == (PARITY, 0) else None
    return Policy(row.key, row.version, row.title, row.document)


def versions(key: str) -> List[dict]:
    from apps.content.models import ContentPolicy

    return [
        {
            "key": row.key,
            "version": row.version,
            "ref": f"{row.key}@{row.version}",
            "title": row.title,
            "updated_by": str(row.updated_by_id) if row.updated_by_id else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in ContentPolicy.objects.filter(key=key).order_by("-version")
    ]


def save(
    key: str, document: Any, user=None, dry_run: bool = False, create: Optional[bool] = None
) -> Tuple[Policy, bool]:
    """Validate and store `document` as the next version of `key`; `(policy, changed)`.

    `create=True` refuses an existing key, `create=False` a missing one. An identical document writes nothing.
    With `dry_run` it validates and returns the version that would be written.
    """
    from apps.content.models import ContentPolicy

    validate(key, document)
    existing = _current_map().get(key)
    if create is True and existing is not None:
        raise PolicyError(f"A content policy '{key}' already exists; update it instead.", path="key")
    if create is False and existing is None:
        raise PolicyError(f"No content policy '{key}'; create it first.", path="key")
    if existing is not None and existing.document == document:
        return existing, False
    if dry_run:
        number = (existing.version if existing else 0) + 1
        return Policy(key, number, document["title"], copy.deepcopy(document)), True

    with transaction.atomic():
        # Locks this key's rows so two saves cannot both write the same next version.
        list(ContentPolicy.objects.select_for_update().filter(key=key).values_list("pk", flat=True))
        number = (ContentPolicy.objects.filter(key=key).aggregate(top=Max("version"))["top"] or 0) + 1
        row = ContentPolicy.objects.create(
            key=key, version=number, title=document["title"], document=document, updated_by=user
        )
        invalidate()
        transaction.on_commit(invalidate)
    logger.info("content_policy.save policy=%s user=%s", f"{key}@{number}", getattr(user, "pk", None))
    return Policy(row.key, row.version, row.title, row.document), True


def ensure_presets() -> List[str]:
    """Create the presets that have no row (version 1), as migration `0002_presets` does; the keys created.

    For a database whose rows were flushed after migrating (a `TransactionTestCase`), or a preset someone
    deleted. Never touches a preset that exists, at any version.
    """
    from apps.content.models import ContentPolicy

    created = []
    for key, document in PRESETS.items():
        if not ContentPolicy.objects.filter(key=key).exists():
            ContentPolicy.objects.create(key=key, version=1, title=document["title"], document=copy.deepcopy(document))
            created.append(key)
    if created:
        invalidate()
    return created


# --- resolution -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    policy: Policy
    #: `entry`, `catalog`, `instance` or `builtin`.
    source: str
    #: Assignments that named no policy and were skipped, as `level:key`.
    skipped: Tuple[str, ...] = ()


class Resolver:
    """Resolves many entries against one read of the policy map and the instance default (one batch)."""

    def __init__(self):
        self.policies = _current_map()
        self.policies.setdefault(PARITY, BUILTIN_PARITY)
        self.default_key = settings_store.get("content.default_policy")

    def resolve(self, entry, catalog=None) -> Resolution:
        """The policy that governs `entry`: entry → catalog → instance setting → built-in `parity`."""
        catalog = catalog if catalog is not None else getattr(entry, "catalog", None)
        candidates = (
            ("entry", (entry.config or {}).get("content_policy") if entry is not None else None),
            ("catalog", getattr(catalog, "content_policy", None)),
            ("instance", self.default_key),
        )
        skipped = []
        for source, key in candidates:
            if not key:
                continue
            policy = self.policies.get(key)
            if policy is not None:
                return Resolution(policy, source, tuple(skipped))
            skipped.append(f"{source}:{key}")
            logger.warning(
                "content_policy.missing source=%s policy=%s entry=%s", source, key, getattr(entry, "pk", None)
            )
        return Resolution(self.policies[PARITY], "builtin", tuple(skipped))


def resolve(entry, catalog=None) -> Resolution:
    """The policy that governs `entry`: entry → catalog → instance setting → built-in `parity`."""
    return Resolver().resolve(entry, catalog)


__all__ = [
    "BASES",
    "BUILTIN_PARITY",
    "PARITY",
    "PRESETS",
    "Policy",
    "PolicyError",
    "Resolution",
    "SCHEMA",
    "TIERS",
    "all_current",
    "current",
    "exists",
    "require",
    "resolve",
    "save",
    "validate",
    "version",
    "versions",
]

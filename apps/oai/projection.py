"""IP-033: which entries are harvestable, and what a harvester sees of one (the projection).

The projection is plain JSON with everything the crosswalks render already resolved (portal URL, external
record URLs, org codes, the OAI identifier). Configuration therefore lives inside it: changing
`content.cite_base_url` or `EVILFLOWERS_EXTERNAL_SYSTEMS` changes the fingerprint, and the next sync re-stamps
the record, as §2.7.1 asks when a record's dissemination changes. Nothing that moves on a read (popularity,
`touched_at`, `rights.updated_at`) may enter it, or every read would re-send the record.
"""

import hashlib
import json
import re
import uuid
from typing import Iterable, Optional

from django.conf import settings
from django.utils.html import strip_tags

from apps.content.citation import entry_url
from apps.core import settings_store
from apps.core.identifiers import external_systems
from apps.core.models import Entry
from apps.harvest.exposure import not_reexposed_origins
from apps.harvest.records import UDC_SCHEME, normalize_ccnb, normalize_doi, normalize_isbn, normalize_issn

#: Bump when a crosswalk's output changes: every fingerprint changes once and harvesters get every record again.
CROSSWALK_VERSION = 1

#: Short names of category schemes, as cataloguers write them ("UDC 621.3").
SCHEME_NAMES = {UDC_SCHEME: "UDC"}

#: The label language a single label is taken in, best first.
LABEL_LANGUAGES = ("sk", "en", "cs")

#: Restricted (Readium LCP, loan-only) titles: `dc:rights` and MARC `506`.
LCP_ACCESS_STATEMENT = (
    "Access restricted: lent to registered library users as a Readium LCP protected e-book (loan only)."
)

_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_LANGUAGE_TAG = re.compile(r"^[a-zA-Z]{1,8}(-[a-zA-Z0-9]{1,8})*$")


def text(value) -> Optional[str]:
    """Single-line text without XML-illegal characters (lxml refuses them, and one would fail a whole page)."""
    if value in (None, ""):
        return None
    cleaned = " ".join(_XML_ILLEGAL.sub(" ", str(value)).split())
    return cleaned or None


def oai_identifier(entry_id) -> str:
    return f"oai:{settings.EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER}:{entry_id}"


def parse_identifier(identifier: Optional[str]) -> Optional[uuid.UUID]:
    """The entry id of an OAI identifier of this repository, or None."""
    prefix = f"oai:{settings.EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER}:"
    if not identifier or not identifier.startswith(prefix):
        return None
    try:
        return uuid.UUID(identifier[len(prefix) :])
    except ValueError:
        return None


def load(entry_ids: Iterable) -> list[Entry]:
    """Entries with everything the exposure rule and the projection read, in a fixed number of queries."""
    return list(
        Entry.objects.filter(pk__in=list(entry_ids))
        .select_related("catalog", "language")
        .prefetch_related("entry_authors__author", "categories", "acquisitions")
    )


def registered_entry_ids(entry_ids: Iterable) -> set:
    """Entries with a package registered with the LCP server: the rule `lcp_state_mapping` lends by."""
    from apps.readium.models import EncryptedContent

    return set(
        EncryptedContent.objects.filter(
            acquisition__entry_id__in=list(entry_ids),
            status=EncryptedContent.EncryptionStatus.REGISTERED,
            encrypted_at__isnull=False,
        ).values_list("acquisition__entry_id", flat=True)
    )


def is_exposed(entry: Entry, registered: set, not_reexposed: Optional[set] = None) -> bool:
    """Q1: a harvestable catalog, a file, a registered LCP package if protected, not imported from a source that
    is an OAI provider itself (Q7: `HarvestSource.oai_reexpose = False`), not withdrawn (IP-034)."""
    if not_reexposed is None:
        not_reexposed = not_reexposed_origins()
    if not entry.catalog.harvestable or entry.origin in not_reexposed or entry.withdrawn_at is not None:
        return False
    if not entry.acquisitions.all():
        return False
    if entry.read_config("readium_enabled") and entry.pk not in registered:
        return False
    return True


def set_specs(entry: Entry) -> list[str]:
    """`<url_name>` and `<url_name>:<category id>` (Q3): ids, so a corrected label never moves a set."""
    catalog = entry.catalog.url_name
    return [catalog] + sorted(f"{catalog}:{category.pk}" for category in entry.categories.all())


def label_of(label, fallback: Optional[str] = None) -> Optional[str]:
    """One label of a category's localised `label`, in the library's language where it has one."""
    if isinstance(label, str):
        return text(label) or fallback
    if not isinstance(label, dict) or not label:
        return fallback
    for language in LABEL_LANGUAGES:
        if text(label.get(language)):
            return text(label[language])
    for key in sorted(label):
        if text(label[key]):
            return text(label[key])
    return fallback


def _labels(label) -> dict:
    if not isinstance(label, dict):
        return {}
    return {key: text(value) for key, value in sorted(label.items()) if _LANGUAGE_TAG.match(key) and text(value)}


def _identifiers(raw: Optional[dict]) -> dict:
    raw = raw or {}
    result = {}
    # Normalised where valid (the resolver matches on the normalised ISBN-13), as stored otherwise.
    for key, normalizer in (
        ("isbn", normalize_isbn),
        ("issn", normalize_issn),
        ("ccnb", normalize_ccnb),
        ("doi", normalize_doi),
    ):
        value = text(raw.get(key))
        if value:
            result[key] = normalizer(value) or value
    return result


def _external_ids(raw: Optional[dict]) -> list[dict]:
    raw = raw or {}
    result = []
    for system in sorted(external_systems(), key=lambda item: item.key):
        record_id = text(raw.get(system.key))
        if not record_id:
            continue
        result.append(
            {
                "key": system.key,
                "name": system.name,
                "record_id": record_id,
                "url": system.record_url(record_id),
                "marc_org_code": system.marc_org_code or system.key,
            }
        )
    return result


def portal_url(entry_id) -> Optional[str]:
    """The portal entry (`content.citation.entry_url`); Q6: none while `content.cite_base_url` is empty."""
    if not (settings_store.get("content.cite_base_url") or "").strip():
        return None
    return entry_url(entry_id)


def project(entry: Entry) -> dict:
    authors = sorted(entry.entry_authors.all(), key=lambda item: item.position)
    subjects = sorted(
        (
            {
                "scheme": category.scheme or None,
                "scheme_name": SCHEME_NAMES.get(category.scheme or ""),
                "term": text(category.term),
                "label": label_of(category.label),
                "labels": _labels(category.label),
            }
            for category in entry.categories.all()
        ),
        key=lambda subject: (subject["scheme"] or "", subject["term"] or ""),
    )
    rights = entry.rights or {}
    licence = {key: text(rights.get(key)) for key in ("license_url", "license_name") if text(rights.get(key))}
    lcp = bool(entry.read_config("readium_enabled"))
    language = entry.language
    return {
        "identifier": oai_identifier(entry.pk),
        "entry_id": str(entry.pk),
        "title": text(entry.title),
        "authors": [
            {"surname": text(item.author.surname) or "", "name": text(item.author.name) or ""} for item in authors
        ],
        "subjects": subjects,
        "identifiers": _identifiers(entry.identifiers),
        "external_ids": _external_ids(entry.identifiers),
        "language": {"alpha2": language.alpha2, "alpha3": language.alpha3} if language else None,
        "publisher": text(entry.publisher),
        "published_at": str(entry.published_at) if entry.published_at else None,
        "edition": text(entry.edition),
        "page_count": entry.page_count or None,
        "summary": text(strip_tags(entry.summary or "")),
        "rights": licence or None,
        "access": {"lcp": True, "statement": LCP_ACCESS_STATEMENT} if lcp else None,
        "formats": sorted({acquisition.mime for acquisition in entry.acquisitions.all() if acquisition.mime}),
        "url": portal_url(entry.pk),
        "created": entry.created_at.date().isoformat() if entry.created_at else None,
        "marc_org_code": settings.EVILFLOWERS_MARC_ORG_CODE,
    }


def fingerprint(projection: dict, specs: list[str]) -> str:
    """SHA-256 of the canonical JSON of what a harvester gets: the projection, its sets and the crosswalk version."""
    payload = {"crosswalk": CROSSWALK_VERSION, "projection": projection, "set_specs": specs}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CROSSWALK_VERSION",
    "LCP_ACCESS_STATEMENT",
    "fingerprint",
    "is_exposed",
    "label_of",
    "load",
    "oai_identifier",
    "parse_identifier",
    "portal_url",
    "project",
    "registered_entry_ids",
    "set_specs",
    "text",
]

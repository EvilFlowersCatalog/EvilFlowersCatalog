"""Rights metadata on entries (#177): the licence and the text-and-data-mining reservation.

`Entry.rights` is a JSON object:

| Key | Meaning |
|---|---|
| `license_url` | The licence the publication is distributed under (e.g. a Creative Commons deed) |
| `license_name` | Its short name (`CC BY 4.0`), where the source gives one |
| `tdm_reservation` | `1` when the rights holder reserves text and data mining (W3C TDMRep `tdm-reservation`), else `0` |
| `tdm_policy_url` | Where the rights holder's TDM licence or policy is published (TDMRep `tdm-policy`) |
| `ai_use` | Free text from the rights holder about AI use, e.g. an AIPREF value (an IETF draft) |
| `source` | Where the facts came from: `dataverse`, `crossref` or `manual` |
| `updated_at` | When they were last written |

Rights never pick a content policy (Q7). A reservation only applies the policy's `caps.tdm_reserved`
(`apps.content.access.tdm_reserved`).

Sources: Dataverse (the dataset's licence) on sync, Crossref (`license[]` of the DOI) through
`refresh_crossref`, and manual edits over REST and MCP. A manual edit is never overwritten by an automatic
source.
"""

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, Optional

import requests
from django.utils import timezone

from apps.core import settings_store

logger = logging.getLogger(__name__)

SOURCES = ("dataverse", "crossref", "manual")
FIELDS = ("license_url", "license_name", "tdm_reservation", "tdm_policy_url", "ai_use")

CROSSREF_URL = "https://api.crossref.org/works/{doi}"
#: A generic client name; Crossref's polite pool asks for a contact, which this deployment does not volunteer.
CROSSREF_USER_AGENT = "EvilFlowersCatalog (+https://github.com/EvilFlowersCatalog/EvilFlowersCatalog)"
CROSSREF_TIMEOUT = 10
#: Crossref `content-version`s that describe the published text, best first; `tdm` names a TDM licence.
CROSSREF_VERSIONS = ("vor", "unspecified", "am")

#: The JSON Schema of `rights` as REST and MCP accept it (the `source` and `updated_at` are set by the server).
SCHEMA: Dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "license_url": {"type": ["string", "null"], "maxLength": 500, "description": "URL of the licence."},
        "license_name": {"type": ["string", "null"], "maxLength": 200},
        "tdm_reservation": {
            "type": ["integer", "null"],
            "enum": [0, 1, None],
            "description": "1: the rights holder reserves text and data mining (TDMRep).",
        },
        "tdm_policy_url": {"type": ["string", "null"], "maxLength": 500},
        "ai_use": {"type": ["string", "null"], "maxLength": 200},
    },
}


class RightsError(ValueError):
    pass


def _url(value: Any, name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not value.lower().startswith(("http://", "https://")) or len(value) > 500:
        raise RightsError(f"`rights.{name}` must be an http(s) URL of at most 500 characters.")
    return value


def clean(data: Any, source: str) -> Optional[dict]:
    """Validate `data` and stamp it with `source`; None clears the rights."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise RightsError("`rights` must be an object.")
    unknown = sorted(set(data) - set(FIELDS) - {"source", "updated_at"})
    if unknown:
        raise RightsError(f"Unknown `rights` keys: {', '.join(unknown)}. Allowed: {', '.join(FIELDS)}.")
    reservation = data.get("tdm_reservation")
    if reservation in (True, "1"):
        reservation = 1
    elif reservation in (False, "0"):
        reservation = 0
    if reservation not in (0, 1, None):
        raise RightsError("`rights.tdm_reservation` is 0 or 1.")
    cleaned = {
        "license_url": _url(data.get("license_url"), "license_url"),
        "license_name": (str(data["license_name"]).strip()[:200] or None) if data.get("license_name") else None,
        "tdm_reservation": reservation,
        "tdm_policy_url": _url(data.get("tdm_policy_url"), "tdm_policy_url"),
        "ai_use": (str(data["ai_use"]).strip()[:200] or None) if data.get("ai_use") else None,
    }
    cleaned = {key: value for key, value in cleaned.items() if value is not None}
    if not cleaned:
        return None
    return {**cleaned, "source": source, "updated_at": timezone.now().isoformat()}


def merge(current: Optional[dict], incoming: Optional[dict]) -> Optional[dict]:
    """`incoming` (already `clean`ed) over `current`, unless `current` was edited by hand and `incoming` is not."""
    if incoming is None:
        return current
    if current and current.get("source") == "manual" and incoming.get("source") != "manual":
        return current
    return incoming


def apply(entry, incoming: Optional[dict]) -> bool:
    """Store `incoming` on `entry` by the merge rule, with a targeted update; whether anything changed."""
    from apps.core.models import Entry

    merged = merge(entry.rights, incoming)
    if _facts(merged) == _facts(entry.rights) and (merged or {}).get("source") == (entry.rights or {}).get("source"):
        return False
    entry.rights = merged
    # `updated_at` too: OPDS feeds revalidate on it, and they show the licence.
    Entry.objects.filter(pk=entry.pk).update(rights=merged, updated_at=timezone.now())
    from apps.content import tdmrep

    tdmrep.invalidate()
    return True


def _facts(rights: Optional[dict]) -> dict:
    return {key: value for key, value in (rights or {}).items() if key != "updated_at"}


# -- Dataverse -------------------------------------------------------------------------------------------------


def from_dataverse(dataset_data: Dict[str, Any]) -> Optional[dict]:
    """The dataset version's licence (`license: {name, uri}` since Dataverse 5.10), or None."""
    license = dataset_data.get("license")
    if isinstance(license, dict) and (license.get("uri") or license.get("name")):
        try:
            return clean({"license_url": license.get("uri"), "license_name": license.get("name")}, "dataverse")
        except RightsError:
            return clean({"license_name": license.get("name")}, "dataverse")
    if isinstance(license, str) and license.strip():
        return clean({"license_name": license}, "dataverse")
    return None


# -- Crossref --------------------------------------------------------------------------------------------------


def doi_of(entry) -> Optional[str]:
    doi = (entry.identifiers or {}).get("doi")
    if not doi:
        return None
    doi = doi.strip()
    for prefix in ("doi:", "https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix) :]
    return doi or None


def _started(item: dict, now: datetime) -> bool:
    start = (item.get("start") or {}).get("date-time")
    if not start:
        return True
    try:
        return datetime.fromisoformat(start.replace("Z", "+00:00")) <= now
    except ValueError:
        return True


def from_crossref_message(message: Dict[str, Any]) -> Optional[dict]:
    """The licence of the published version from a Crossref work's `license[]`, and its TDM licence if any."""
    now = datetime.now(dt_timezone.utc)
    licenses = [item for item in message.get("license") or [] if item.get("URL") and _started(item, now)]
    chosen = None
    for version in CROSSREF_VERSIONS:
        chosen = next((item for item in licenses if item.get("content-version") == version), None)
        if chosen:
            break
    tdm = next((item for item in licenses if item.get("content-version") == "tdm"), None)
    if chosen is None and tdm is None:
        return None
    try:
        return clean(
            {
                "license_url": chosen.get("URL") if chosen else None,
                "tdm_policy_url": tdm.get("URL") if tdm else None,
            },
            "crossref",
        )
    except RightsError:
        return None


class CrossrefUnavailable(Exception):
    """Crossref could not be asked (network, timeout, 429, 5xx): unlike an unknown DOI, worth asking again."""


def crossref_work(doi: str, *, strict: bool = False) -> Optional[Dict[str, Any]]:
    """The Crossref `message` of a DOI, or None (unknown DOI, Crossref down). Call it from Celery only.

    `strict` raises `CrossrefUnavailable` when Crossref was not reachable, so a caller that records having asked
    does not record a failed attempt as an answer.
    """
    try:
        response = requests.get(
            CROSSREF_URL.format(doi=requests.utils.quote(doi, safe="/")),
            headers={"User-Agent": CROSSREF_USER_AGENT, "Accept": "application/json"},
            timeout=CROSSREF_TIMEOUT,
        )
    except requests.RequestException as error:
        logger.warning("crossref.unavailable doi=%s error=%s", doi, error)
        if strict:
            raise CrossrefUnavailable(str(error)) from error
        return None
    if response.status_code != 200:
        if response.status_code != 404:
            logger.warning("crossref.status doi=%s status=%s", doi, response.status_code)
            if strict and (response.status_code == 429 or response.status_code >= 500):
                raise CrossrefUnavailable(f"status {response.status_code}")
        return None
    try:
        return (response.json() or {}).get("message") or None
    except ValueError:
        return None


def refresh_crossref(entry) -> bool:
    """Fetch the DOI's licence from Crossref and store it on `entry` (never over a manual edit)."""
    if not settings_store.get("content.crossref_enabled"):
        return False
    doi = doi_of(entry)
    if not doi:
        return False
    message = crossref_work(doi)
    if message is None:
        return False
    return apply(entry, from_crossref_message(message))


__all__ = [
    "SCHEMA",
    "SOURCES",
    "CrossrefUnavailable",
    "RightsError",
    "apply",
    "clean",
    "crossref_work",
    "doi_of",
    "from_crossref_message",
    "from_dataverse",
    "merge",
    "refresh_crossref",
]

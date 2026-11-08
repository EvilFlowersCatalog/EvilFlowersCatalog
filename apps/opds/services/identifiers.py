"""Publication identifiers as URIs, shared by OPDS 1.2 (`dc:identifier`) and
OPDS 2.0 (`metadata.identifier`)."""

from typing import List

from apps.core.models import Entry


def entry_identifier_uris(entry: Entry) -> List[str]:
    """ISBN and DOI as URIs, most specific first; `[]` when the entry has neither.

    Readers key an imported publication on its identifier, so callers that need
    exactly one fall back to `urn:uuid:<entry id>` (see `primary_identifier`).
    """
    identifiers = entry.identifiers or {}
    uris = []
    if identifiers.get("isbn"):
        uris.append(f"urn:isbn:{identifiers['isbn'].replace('-', '').replace(' ', '')}")
    if identifiers.get("doi"):
        uris.append(f"https://doi.org/{identifiers['doi']}")
    return uris


def primary_identifier(entry: Entry) -> str:
    return next(iter(entry_identifier_uris(entry)), f"urn:uuid:{entry.pk}")

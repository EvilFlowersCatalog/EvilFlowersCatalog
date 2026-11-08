"""Publication identifiers as URIs, shared by OPDS 1.2 (`dc:identifier`),
OPDS 2.0 (`metadata.identifier`) and the LCP package manifest."""

from typing import List

from apps.core.models import Entry

# Imported data sometimes stores the identifier already as a URI; without
# stripping, the feeds and the package manifest said `urn:isbn:urn:isbn:…`.
_ISBN_PREFIXES = ("urn:isbn:", "isbn:")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def _strip_prefix(value: str, prefixes) -> str:
    value = value.strip()
    lowered = value.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def isbn_uri(isbn: str) -> str:
    return f"urn:isbn:{_strip_prefix(isbn, _ISBN_PREFIXES).replace('-', '').replace(' ', '')}"


def doi_uri(doi: str) -> str:
    return f"https://doi.org/{_strip_prefix(doi, _DOI_PREFIXES)}"


def entry_identifier_uris(entry: Entry) -> List[str]:
    """ISBN and DOI as URIs, most specific first; `[]` when the entry has neither.

    Readers key an imported publication on its identifier, so callers that need
    exactly one fall back to `urn:uuid:<entry id>` (see `primary_identifier`).
    """
    identifiers = entry.identifiers or {}
    uris = []
    if identifiers.get("isbn"):
        uris.append(isbn_uri(identifiers["isbn"]))
    if identifiers.get("doi"):
        uris.append(doi_uri(identifiers["doi"]))
    return uris


def primary_identifier(entry: Entry) -> str:
    return next(iter(entry_identifier_uris(entry)), f"urn:uuid:{entry.pk}")

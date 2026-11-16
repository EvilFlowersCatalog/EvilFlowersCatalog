"""The citation object (#176): an address of a passage, region or section, not a copy of it.

```json
{
  "uri": "evilflowers://entry/<id>/locator/<key>",
  "url": "https://<portal>/cite/<id>/<key>",
  "work": {"type": "book", "title": "…", "author": [{"family": "…", "given": "…"}], "ISBN": "…", "DOI": "…"},
  "locator": {"page": 14, "source_page": 13, "section": "2.3 Stability", "label": "Fig. 3"},
  "selector": {"type": "TextQuoteSelector", "exact": "…", "prefix": "…", "suffix": "…"},
  "readium_locator": {"href": "…", "type": "application/pdf", "locations": {"position": 14, "fragments": ["page=14"]}},
  "resolution": "exact",
  "access": {"tier": "snippet", "basis": "none", "policy": "parity@1", "how_to_unlock": ["borrow: <url>"]}
}
```

- `work` is CSL-JSON from the entry's metadata.
- The quote (`selector.exact`, the Readium `text`) appears only at tier ≥ `snippet`; the section heading only at
  ≥ `outline`. Whoever resolves a citation passes the gate under their own access.
- `url` is built on the setting `content.cite_base_url`; the portal's `/cite` route opens the viewer there.

`cite()` formats references from the same objects: ISO 690, APA, BibTeX and CSL-JSON.
"""

import re
from typing import Iterable, List, Optional

from apps.content import access
from apps.core import settings_store

STYLES = ("iso690", "apa", "bibtex", "csl-json")

#: A printed caption's label: "Obr. 2.1", "Figure 3", "Tab. 4.2", "Rovnica (5)".
_LABEL = re.compile(r"^\s*([^\W\d_][\w.]{0,15}\s*\(?[0-9IVXLC]+(?:[.\-][0-9]+)*\)?)", re.UNICODE)


def cite_url(entry_id, key: str) -> str:
    base = (settings_store.get("content.cite_base_url") or "").rstrip("/")
    return f"{base}/cite/{entry_id}/{key}"


def entry_url(entry_id) -> str:
    """Where a reader borrows or opens the publication: the portal's entry detail (a dialog over the library)."""
    base = (settings_store.get("content.cite_base_url") or "").rstrip("/")
    return f"{base}/library?entry-detail-id={entry_id}"


def label_of(caption: Optional[str]) -> Optional[str]:
    """The printed label of a region ("Obr. 2.1") from its caption."""
    match = _LABEL.match(caption or "")
    return " ".join(match.group(1).split()) if match else None


def _issued(entry) -> Optional[dict]:
    if not entry.published_at:
        return None
    parts = []
    for part in str(entry.published_at).split("-")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            break
    return {"date-parts": [parts]} if parts else None


def work(entry) -> dict:
    """CSL-JSON of a publication from its catalogue record."""
    identifiers = entry.identifiers or {}
    authors = sorted(entry.entry_authors.all(), key=lambda item: item.position)
    csl = {
        "id": str(entry.pk),
        "type": "book",
        "title": entry.title,
        "author": [{"family": item.author.surname, "given": item.author.name} for item in authors],
        "publisher": entry.publisher or None,
        "issued": _issued(entry),
        "ISBN": _isbn(identifiers.get("isbn")),
        "DOI": _doi(identifiers.get("doi")),
        "language": entry.language.alpha2 if entry.language_id and entry.language else None,
        "number-of-pages": entry.page_count or None,
    }
    return {key: value for key, value in csl.items() if value not in (None, [], "")}


def _isbn(value: Optional[str]) -> Optional[str]:
    """The ISBN as printed in a reference: hyphenated ISBN-13 (`978-3-319-29792-7`), never the stored URN (#186).

    An ISBN that fails its checksum is kept as stored, only without a `urn:isbn:` / `ISBN` prefix.
    """
    from apps.harvest.records import _ISBN_PREFIX, normalize_isbn

    if not value or not value.strip():
        return None
    digits = normalize_isbn(value)
    if digits is None:
        return _ISBN_PREFIX.sub("", value.strip()) or None
    try:
        import isbnlib

        return isbnlib.mask(digits) or digits
    except Exception:  # noqa: BLE001 - a range the bundled table does not know: the bare digits
        return digits


def _doi(value: Optional[str]) -> Optional[str]:
    """The bare DOI (`10.1007/…`), without `doi:`, `urn:doi:` or a resolver URL (#186)."""
    if not value:
        return None
    value = value.strip()
    for prefix in (
        "urn:doi:",
        "doi:",
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
    ):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    return value or None


def access_of(decision: access.Disclosure, entry_id) -> dict:
    """The `access` block: the caller's tier and how to get more."""
    unlock = []
    for way in decision.how_to_unlock:
        if way == "borrow":
            unlock.append(f"borrow: {entry_url(entry_id)}")
        elif way == "campus_network":
            unlock.append("campus_network: read it from the library's network")
        elif way == "local_processor":
            unlock.append("local_processor: use a key whose processor is `local` (the library's own model)")
        else:
            unlock.append(way)
    payload = {
        "tier": decision.tier,
        "basis": decision.basis,
        "reason": decision.reason or None,
        "policy": decision.policy,
        "how_to_unlock": unlock,
    }
    if decision.metered:
        payload["budget_left"] = decision.budget_left
    return payload


def build(request, entry, resolution, decision: access.Disclosure) -> dict:
    """The citation object of a resolved locator (`apps.extraction.locators.Resolution`) for this caller."""
    from apps.extraction.locator_views import serialize
    from apps.mcp import uris

    locator = resolution.locator
    readable = decision.allows(access.Tier.SNIPPET)
    resolved = serialize(request, entry, resolution, readable)

    section = None
    if decision.allows(access.Tier.OUTLINE):
        if resolution.paragraph is not None:
            section = resolution.paragraph.section
        elif resolution.section is not None:
            section = resolution.section.title
    label = None
    if resolution.region is not None:
        label = label_of(resolution.region.printed_caption or resolution.region.caption)
    elif getattr(resolution, "bibitem", None) is not None:
        label = f"[{resolution.bibitem.number}]" if resolution.bibitem.number else None
    elif locator.kind not in ("paragraph", "section", "bibitem"):
        label = label_of(locator.quote_exact)

    readium = {key: value for key, value in resolved["readium"].items() if value not in (None, {}, "")}
    citation = {
        "uri": uris.locator_uri(entry.pk, locator.key),
        "url": cite_url(entry.pk, locator.key),
        "kind": locator.kind,
        "work": work(entry),
        "locator": {
            key: value
            for key, value in {
                "page": resolved["page"],
                "source_page": resolved["source_page"],
                "section": section,
                "label": label,
            }.items()
            if value is not None
        },
        "readium_locator": readium,
        "resolution": resolution.resolution,
        "access": access_of(decision, entry.pk),
    }
    if getattr(resolution, "bibitem", None) is not None:
        from apps.extraction.bibliography import reference_payload

        # An item of the book's reference list (#178): the work it cites, and the library's copy if any.
        citation["reference"] = reference_payload(request, resolution.bibitem, readable)
    if readable and resolved.get("quote"):
        citation["selector"] = {
            "type": "TextQuoteSelector",
            "exact": resolved["quote"],
            "prefix": locator.prefix or "",
            "suffix": locator.suffix or "",
        }
    return citation


# -- formatting ------------------------------------------------------------------------------------------------


def _year(csl: dict) -> Optional[str]:
    parts = (csl.get("issued") or {}).get("date-parts") or [[]]
    return str(parts[0][0]) if parts and parts[0] else None


def _iso690_names(authors: List[dict]) -> str:
    names = [f"{author.get('family', '').upper()}, {author.get('given', '')}".strip(", ") for author in authors]
    if len(names) > 3:
        return f"{names[0]} et al."
    return "; ".join(names)


def _apa_names(authors: List[dict]) -> str:
    def one(author):
        initials = " ".join(f"{part[0]}." for part in (author.get("given") or "").split() if part)
        return f"{author.get('family', '')}, {initials}".strip(", ")

    names = [one(author) for author in authors]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + ", & " + names[-1]


def _pages(citation: dict) -> Optional[str]:
    page = (citation.get("locator") or {}).get("page")
    return str(page) if page else None


def iso690(citation: dict) -> str:
    csl = citation["work"]
    parts = []
    names = _iso690_names(csl.get("author") or [])
    if names:
        parts.append(names + ".")
    parts.append(f"{csl['title']}.")
    publication = ", ".join(value for value in (csl.get("publisher"), _year(csl)) if value)
    if publication:
        parts.append(publication + ".")
    if csl.get("ISBN"):
        parts.append(f"ISBN {csl['ISBN']}.")
    if csl.get("DOI"):
        parts.append(f"DOI {csl['DOI']}.")
    if _pages(citation):
        parts.append(f"S. {_pages(citation)}.")
    parts.append(f"Dostupné na: {citation['url']}")
    return " ".join(parts)


def apa(citation: dict) -> str:
    csl = citation["work"]
    names = _apa_names(csl.get("author") or [])
    year = _year(csl) or "n.d."
    head = f"{names} ({year})." if names else f"{csl['title']}. ({year})."
    parts = [head]
    if names:
        parts.append(f"{csl['title']}.")
    if csl.get("publisher"):
        parts.append(f"{csl['publisher']}.")
    if _pages(citation):
        parts.append(f"p. {_pages(citation)}.")
    parts.append(f"https://doi.org/{csl['DOI']}" if csl.get("DOI") else citation["url"])
    return " ".join(parts)


_KEY_STOPWORDS = {"a", "an", "the", "on", "of", "to", "and", "o", "na", "v", "k", "der", "die", "das"}


def _bibtex_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def bibtex(citation: dict) -> str:
    csl = citation["work"]
    authors = " and ".join(
        f"{author.get('family', '')}, {author.get('given', '')}".strip(", ") for author in csl.get("author") or []
    )
    # No author: the title's first word (BibTeX's own convention), not "anon" (#186).
    first = (csl.get("author") or [{}])[0].get("family") or next(
        (word for word in re.findall(r"\w+", csl.get("title") or "") if word.lower() not in _KEY_STOPWORDS), "anon"
    )
    key = re.sub(r"[^A-Za-z0-9]", "", first) + (_year(csl) or "") + citation["uri"].rsplit("/", 1)[-1][:6]
    fields = {
        "title": csl.get("title"),
        "author": authors or None,
        "publisher": csl.get("publisher"),
        "year": _year(csl),
        "isbn": csl.get("ISBN"),
        "doi": csl.get("DOI"),
        "pages": _pages(citation),
        "url": citation["url"],
    }
    body = ",\n".join(f"  {name} = {{{_bibtex_escape(value)}}}" for name, value in fields.items() if value)
    return f"@book{{{key},\n{body}\n}}"


def cite(citations: Iterable[dict], style: str) -> List[dict]:
    """Each citation formatted in `style` (`iso690`, `apa`, `bibtex`, `csl-json`)."""
    formatted = []
    for item in citations:
        if style == "csl-json":
            text = None
            csl = {**item["work"], "URL": item["url"]}
            if _pages(item):
                csl["page"] = _pages(item)
            formatted.append({"uri": item["uri"], "csl": csl})
            continue
        text = {"iso690": iso690, "apa": apa, "bibtex": bibtex}[style](item)
        formatted.append({"uri": item["uri"], "text": text})
    return formatted


__all__ = ["STYLES", "access_of", "build", "cite", "cite_url", "label_of", "work"]

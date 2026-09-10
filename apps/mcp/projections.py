"""ORM instances -> the compact JSON objects tools hand back to a model.

These deliberately do **not** reuse `apps.api.serializers`. The REST
serializers are shaped for the portal: aliased keys, every field present even
when null, the full `config` blob, base64-adjacent payloads. All of that is
context the model pays for and cannot use. The projections below emit short
keys, drop nulls, truncate long prose, and resolve every URL to an absolute one
so a tool result is directly actionable.

Everything returned here is a JSON primitive (`str`, `int`, `bool`, `None`,
`list`, `dict`) — UUIDs are stringified and datetimes ISO-formatted at the
boundary, so the transport needs no custom JSON encoder.
"""

from datetime import datetime
from typing import Optional

from django.conf import settings

from apps.core.models import Acquisition, Author, Catalog, Category, Entry, Feed
from apps.mcp import uris


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _uuid(value) -> Optional[str]:
    return str(value) if value else None


def _compact(payload: dict) -> dict:
    """Drop `None` and empty collections — absent means "nothing to say"."""
    return {key: value for key, value in payload.items() if value not in (None, [], {}, "")}


def _truncate(text: Optional[str], limit: int) -> tuple[Optional[str], bool]:
    if not text:
        return None, False
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return f"{text[:limit].rstrip()}…", True


def author_name(author: Author) -> str:
    return f"{author.name} {author.surname}".strip()


def catalog(instance: Catalog) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "title": instance.title,
            "url_name": instance.url_name,
            "is_public": instance.is_public,
            "resource_uri": uris.catalog_uri(instance.pk),
        }
    )


def _entry_count(instance: Feed) -> int:
    """Prefer the list view's annotation; fall back to a COUNT for a single feed.

    `list_feeds` annotates the queryset, so a page of feeds costs one query. A
    single-feed projection has no annotation and one extra COUNT is cheaper than
    making every caller remember to add one.
    """
    annotated = getattr(instance, "entry_count", None)
    return annotated if annotated is not None else instance.entries.count()


def feed(instance: Feed, request) -> dict:
    """A feed, including where it sits in the navigation tree.

    `entry_count` is read from the prefetch when the caller supplied one and
    falls back to a COUNT otherwise — a feed list would otherwise be one extra
    query per row.
    """
    return _compact(
        {
            "id": _uuid(instance.pk),
            "catalog_id": _uuid(instance.catalog_id),
            "creator_id": _uuid(instance.creator_id),
            "title": instance.title,
            "url_name": instance.url_name,
            "kind": instance.kind,
            "content": instance.content,
            "per_page": instance.per_page,
            "entry_count": _entry_count(instance),
            "parent_ids": [str(pk) for pk in instance.parents.values_list("id", flat=True)],
            "children_ids": [str(pk) for pk in instance.children.values_list("id", flat=True)],
            "opds_url": request.build_absolute_uri(instance.url),
            "resource_uri": uris.feed_uri(instance.pk),
            "created_at": _iso(instance.created_at),
            "updated_at": _iso(instance.updated_at),
        }
    )


def author(instance: Author) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "catalog_id": _uuid(instance.catalog_id),
            "name": instance.name,
            "surname": instance.surname,
            "full_name": author_name(instance),
        }
    )


def category(instance: Category) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "catalog_id": _uuid(instance.catalog_id),
            "term": instance.term,
            "label": instance.label,
            "scheme": instance.scheme,
        }
    )


def acquisition(instance: Acquisition, request) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "relation": instance.relation,
            "mime": instance.mime,
            # Same `Acquisition.url` shim the REST serializer uses. It is a
            # link, not a grant: the download endpoint behind it enforces its
            # own ACL, so handing it to an agent exposes nothing extra.
            "download_url": request.build_absolute_uri(instance.url) if instance.url else None,
        }
    )


def availability(lcp_row: Optional[dict]) -> Optional[dict]:
    """Project one row of `lcp_state_mapping` into the shape a model can reason about.

    Returns `None` for entries with no LCP involvement at all so unprotected
    publications do not each carry ten zero-valued keys.
    """
    if not lcp_row:
        return None

    state = lcp_row.get("lcp_state")
    state = getattr(state, "value", state)
    if state in (None, "not_lcp"):
        return None

    return _compact(
        {
            "state": state,
            "available_slots": lcp_row.get("available_slots"),
            "total_slots": lcp_row.get("total_slots"),
            "next_available_at": _iso(lcp_row.get("next_available_at")),
            "queue_length": lcp_row.get("queue_length"),
            "user_active_license_id": _uuid(lcp_row.get("user_active_license_id")),
            "user_reservation_id": _uuid(lcp_row.get("user_reservation_id")),
            "user_queue_position": lcp_row.get("user_position"),
        }
    )


def entry_summary(
    instance: Entry,
    request,
    lcp_states: Optional[dict] = None,
    shelf_records: Optional[dict] = None,
) -> dict:
    """A search hit: enough to decide whether this is the right publication."""
    lcp_states = lcp_states or {}
    shelf_records = shelf_records or {}

    summary, summary_truncated = _truncate(instance.summary, settings.EVILFLOWERS_MCP_SUMMARY_MAX_CHARS)

    return _compact(
        {
            "id": _uuid(instance.pk),
            "catalog_id": _uuid(instance.catalog_id),
            "title": instance.title,
            "authors": [
                author_name(link.author) for link in sorted(instance.entry_authors.all(), key=lambda a: a.position)
            ],
            "published_at": str(instance.published_at) if instance.published_at else None,
            "publisher": instance.publisher,
            "language": instance.language.alpha2 if instance.language else None,
            "categories": [item.term for item in instance.categories.all()],
            "summary": summary,
            "summary_truncated": summary_truncated or None,
            "page_count": instance.page_count,
            "popularity": instance.popularity or None,
            "availability": availability(lcp_states.get(instance.pk)),
            "on_my_shelf": True if instance.pk in shelf_records else None,
            "resource_uri": uris.entry_uri(instance.pk),
        }
    )


def entry_detail(
    instance: Entry,
    request,
    lcp_states: Optional[dict] = None,
    shelf_records: Optional[dict] = None,
) -> dict:
    """Everything `get_entry` adds on top of a search hit."""
    payload = entry_summary(instance, request, lcp_states, shelf_records)
    content, content_truncated = _truncate(instance.content, settings.EVILFLOWERS_MCP_CONTENT_MAX_CHARS)
    summary, _summary_truncated = _truncate(instance.summary, settings.EVILFLOWERS_MCP_CONTENT_MAX_CHARS)

    payload.update(
        _compact(
            {
                # The detail view affords a longer summary than a search hit.
                "summary": summary,
                "content": content,
                "content_truncated": content_truncated or None,
                "identifiers": dict(instance.identifiers) if instance.identifiers else None,
                "citation": instance.citation,
                "table_of_contents": instance.table_of_contents,
                "cover_url": request.build_absolute_uri(instance.image_url) if instance.image_url else None,
                "acquisitions": [acquisition(item, request) for item in instance.acquisitions.all()],
                "readium_enabled": bool(instance.read_config("readium_enabled")) or None,
                "created_at": _iso(instance.created_at),
                "updated_at": _iso(instance.updated_at),
            }
        )
    )
    if not content_truncated:
        payload.pop("content_truncated", None)
    return payload


def license_summary(instance, request) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "entry_id": _uuid(instance.entry_id),
            "entry_title": instance.entry.title,
            "state": instance.state,
            "starts_at": _iso(instance.starts_at),
            "expires_at": _iso(instance.expires_at),
            "is_expired": instance.is_expired,
            "renewal_count": instance.renewal_count,
        }
    )


def shelf_record(instance, request, lcp_states: Optional[dict] = None) -> dict:
    return _compact(
        {
            "id": _uuid(instance.pk),
            "added_at": _iso(instance.created_at),
            "entry": entry_summary(instance.entry, request, lcp_states),
        }
    )


__all__ = [
    "acquisition",
    "author",
    "author_name",
    "availability",
    "catalog",
    "category",
    "entry_detail",
    "entry_summary",
    "feed",
    "license_summary",
    "shelf_record",
]

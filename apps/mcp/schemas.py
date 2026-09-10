"""Shared JSON Schema fragments for tool inputs and outputs.

Declaring `outputSchema` is a promise: a client may validate `structuredContent`
against it and reject the result if it does not conform. The projections in
`projections.py` omit fields that have nothing to say, so these schemas mark as
`required` only what is genuinely always present, and leave
`additionalProperties` open so adding a field to a projection cannot break a
strict client mid-release.
"""

from typing import Optional

UUID_SCHEMA = {"type": "string", "format": "uuid"}

PAGINATION_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer"},
        "limit": {"type": "integer"},
        "pages": {"type": "integer"},
        "total": {"type": "integer"},
        "has_next_page": {"type": "boolean"},
    },
    "required": ["page", "limit", "pages", "total", "has_next_page"],
}

AVAILABILITY_SCHEMA = {
    "type": "object",
    "description": "Present only for Readium LCP (borrowable) publications.",
    "properties": {
        "state": {
            "type": "string",
            "enum": ["available_now", "available_in_days", "active_loan_for_user", "fully_borrowed"],
        },
        "available_slots": {"type": "integer"},
        "total_slots": {"type": "integer"},
        "next_available_at": {"type": "string", "format": "date-time"},
        "queue_length": {"type": "integer"},
        "user_active_license_id": UUID_SCHEMA,
        "user_reservation_id": UUID_SCHEMA,
        "user_queue_position": {"type": "integer"},
    },
    "required": ["state"],
}

ENTRY_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "catalog_id": UUID_SCHEMA,
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "published_at": {"type": "string"},
        "publisher": {"type": "string"},
        "language": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "summary_truncated": {"type": "boolean"},
        "page_count": {"type": "integer"},
        "popularity": {"type": "integer"},
        "availability": AVAILABILITY_SCHEMA,
        "on_my_shelf": {"type": "boolean"},
        "resource_uri": {"type": "string"},
    },
    "required": ["id", "catalog_id", "title"],
}

ENTRY_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        **ENTRY_SUMMARY_SCHEMA["properties"],
        "content": {"type": "string"},
        "content_truncated": {"type": "boolean"},
        "identifiers": {"type": "object"},
        "citation": {"type": "string"},
        "table_of_contents": {"type": "array"},
        "cover_url": {"type": "string"},
        "acquisitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": UUID_SCHEMA,
                    "relation": {"type": "string"},
                    "mime": {"type": "string"},
                    "download_url": {"type": "string"},
                },
                "required": ["id", "mime"],
            },
        },
        "readium_enabled": {"type": "boolean"},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "catalog_id", "title"],
}

CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "title": {"type": "string"},
        "url_name": {"type": "string"},
        "is_public": {"type": "boolean"},
        "resource_uri": {"type": "string"},
    },
    "required": ["id", "title", "url_name"],
}

AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "catalog_id": UUID_SCHEMA,
        "name": {"type": "string"},
        "surname": {"type": "string"},
        "full_name": {"type": "string"},
    },
    "required": ["id", "catalog_id"],
}

CATEGORY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "catalog_id": UUID_SCHEMA,
        "term": {"type": "string"},
        "label": {"type": "string"},
        "scheme": {"type": "string"},
    },
    "required": ["id", "catalog_id", "term"],
}

FEED_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "catalog_id": UUID_SCHEMA,
        "creator_id": UUID_SCHEMA,
        "title": {"type": "string"},
        "url_name": {"type": "string"},
        "kind": {"type": "string", "enum": ["navigation", "acquisition"]},
        "content": {"type": "string"},
        "per_page": {"type": "integer"},
        "entry_count": {"type": "integer"},
        "parent_ids": {"type": "array", "items": UUID_SCHEMA},
        "children_ids": {"type": "array", "items": UUID_SCHEMA},
        "opds_url": {"type": "string"},
        "resource_uri": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
    "required": ["id", "catalog_id", "title", "url_name", "kind"],
}

LICENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": UUID_SCHEMA,
        "entry_id": UUID_SCHEMA,
        "entry_title": {"type": "string"},
        "state": {"type": "string"},
        "starts_at": {"type": "string", "format": "date-time"},
        "expires_at": {"type": "string", "format": "date-time"},
        "is_expired": {"type": "boolean"},
        "renewal_count": {"type": "integer"},
    },
    "required": ["id", "entry_id", "state"],
}


def list_output(item_schema: dict) -> dict:
    """Output schema for a paginated `{items, metadata}` result."""
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "metadata": PAGINATION_METADATA_SCHEMA,
        },
        "required": ["items", "metadata"],
    }


def single_output(key: str, item_schema: dict, *, extra: Optional[dict] = None) -> dict:
    """Output schema for a `{<key>: {...}}` result."""
    properties = {key: item_schema}
    properties.update(extra or {})
    return {"type": "object", "properties": properties, "required": [key]}


DELETION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "deleted": {"type": "boolean"},
        "id": UUID_SCHEMA,
        "title": {"type": "string"},
    },
    "required": ["deleted", "id"],
}


__all__ = [
    "AUTHOR_SCHEMA",
    "AVAILABILITY_SCHEMA",
    "CATALOG_SCHEMA",
    "CATEGORY_SCHEMA",
    "DELETION_OUTPUT_SCHEMA",
    "ENTRY_DETAIL_SCHEMA",
    "ENTRY_SUMMARY_SCHEMA",
    "FEED_SCHEMA",
    "LICENSE_SCHEMA",
    "PAGINATION_METADATA_SCHEMA",
    "UUID_SCHEMA",
    "list_output",
    "single_output",
]

from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

if TYPE_CHECKING:
    from django_api_forms import Form
    from django_filters import FilterSet


class InstanceDetails(TypedDict):
    """A `return X(...)` / `raise X(...)` as written: the callee and its arguments as source text."""

    constructor: str | None  # None: not a call (`return response`, `raise`)
    kwargs: dict[str, str]
    args: list[str]


class ParameterLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class ExtractionResult(NamedTuple):
    returns: list[InstanceDetails]
    raises: list[InstanceDetails]
    filters: list["type[FilterSet]"]
    form: "type[Form] | None"


OPENAPI_TYPES: dict[str, dict[str, str]] = {
    "str": {"type": "string"},
    "string": {"type": "string"},
    "bool": {"type": "boolean"},
    "boolean": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "int": {"type": "integer", "format": "int32"},
    "integer": {"type": "integer", "format": "int32"},
    "float": {"type": "number", "format": "float"},
    "double": {"type": "number", "format": "double"},
    "decimal": {"type": "string", "format": "decimal"},
    "datetime": {"type": "string", "format": "date-time"},
    "date": {"type": "string", "format": "date"},
    "time": {"type": "string", "format": "time"},
    "duration": {"type": "string", "format": "duration"},
    "email": {"type": "string", "format": "email"},
    "uri": {"type": "string", "format": "uri"},
    "url": {"type": "string", "format": "uri"},
    "slug": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
    "path": {"type": "string"},
    "json": {"type": "object"},
    "binary": {"type": "string", "format": "binary"},
    "byte": {"type": "string", "format": "byte"},
    "password": {"type": "string", "format": "password"},
    "ipv4": {"type": "string", "format": "ipv4"},
    "ipv6": {"type": "string", "format": "ipv6"},
}


# Enhanced schema templates for common use cases
COMMON_SCHEMAS: dict[str, dict[str, Any]] = {
    "Error": {
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "Error message"},
            "code": {"type": "integer", "description": "Error code"},
            "details": {"type": "object", "description": "Additional error details"},
        },
        "required": ["error", "code"],
    },
    "ValidationError": {
        "type": "object",
        "properties": {
            "field_errors": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
                "description": "Field-specific validation errors",
            },
            "non_field_errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "General validation errors",
            },
        },
    },
    "PaginationMetadata": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "minimum": 1, "description": "Current page number"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Items per page"},
            "total": {"type": "integer", "minimum": 0, "description": "Total number of items"},
            "has_next": {"type": "boolean", "description": "Whether there is a next page"},
            "has_previous": {"type": "boolean", "description": "Whether there is a previous page"},
        },
        "required": ["page", "limit", "total", "has_next", "has_previous"],
    },
    "PaginatedResponse": {
        "type": "object",
        "properties": {
            "metadata": {"$ref": "#/components/schemas/PaginationMetadata"},
            "items": {"type": "array", "items": {"type": "object"}, "description": "List of items"},
        },
        "required": ["metadata", "items"],
    },
}


# HTTP Status code descriptions
HTTP_STATUS_DESCRIPTIONS: dict[int, str] = {
    200: "Success",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


# Content type mappings
CONTENT_TYPE_MAPPINGS: dict[str, dict[str, Any]] = {
    "application/json": {"schema": {"type": "object"}},
    "application/xml": {"schema": {"type": "string"}},
    "text/xml": {"schema": {"type": "string"}},
    "application/atom+xml": {"schema": {"type": "string"}},
    "application/opds+json": {"schema": {"type": "object"}},
    "application/webpub+json": {"schema": {"type": "object"}},
    "application/vnd.readium.license.status.v1.0+json": {"schema": {"type": "object"}},
    "application/vnd.readium.lcp.license.v1.0+json": {"schema": {"type": "object"}},
    "image/*": {"schema": {"type": "string", "format": "binary"}},
    "text/event-stream": {"schema": {"type": "string"}},
    "text/plain": {"schema": {"type": "string"}},
    "text/html": {"schema": {"type": "string"}},
    "application/pdf": {"schema": {"type": "string", "format": "binary"}},
    "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
    "image/png": {"schema": {"type": "string", "format": "binary"}},
    "image/gif": {"schema": {"type": "string", "format": "binary"}},
    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
    "multipart/form-data": {"schema": {"type": "object"}},
    "application/x-www-form-urlencoded": {"schema": {"type": "object"}},
}

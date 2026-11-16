"""Annotations a view method carries for the OpenAPI document (read by `apps.openapi.schema.OpenApiDocument`).

Each decorator records attributes on the function itself and returns it: no wrapper, so decorators stack in any
order and nothing is lost between layers. (The earlier wrappers copied `__dict__` before writing to it, which
silently dropped every `@openapi.response`.)
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import Any, Protocol

from pydantic import BaseModel

from apps.openapi.types import OPENAPI_TYPES, ParameterLocation


class Decorator(Protocol):
    """Returns the function it is given, type and all."""

    def __call__[F: Callable[..., Any]](self, func: F, /) -> F: ...


def _annotate(**attributes: Any) -> Decorator:
    def decorator[F: Callable[..., Any]](func: F) -> F:
        vars(func).update(attributes)
        return func

    return decorator


def _openapi(func: Callable[..., Any]) -> dict[str, Any]:
    """The mutable `_openapi` record (responses, examples) of one function, created on first use."""
    return vars(func).setdefault("_openapi", {"responses": {}, "examples": {}})


def openapi_response(
    status_code: HTTPStatus, description: str, serializer: type[BaseModel] | None = None
) -> Decorator:
    """A response the source analysis cannot see, e.g. one built by a helper or a storage backend."""

    def decorator[F: Callable[..., Any]](func: F) -> F:
        _openapi(func)["responses"][status_code] = {"description": description, "serializer": serializer}
        return func

    return decorator


def openapi_metadata(description: str = "", tags: list[str] | None = None, summary: str | None = None) -> Decorator:
    """Description (Markdown), section (`tags`, declared in `apps/openapi/sections.py`) and sidebar label."""
    return _annotate(_openapi_description=description, _openapi_tags=tags or [], _openapi_summary=summary)


def openapi_parameter[T](
    name: str, param_type: type, location: ParameterLocation, description: str, required: bool = True
) -> Callable[[T], T]:
    """A parameter the analysis cannot see (a header, a query parameter read by hand); on a view or a method."""

    def decorator(obj: T) -> T:
        # Its own list, never a base view's: a class's `vars()` is read-only, hence `setattr`.
        parameters = vars(obj).get("_openapi_parameters")
        if parameters is None:
            parameters = []
            setattr(obj, "_openapi_parameters", parameters)  # noqa: B010 - `obj` is a generic `T` to type checkers
        parameters.append(
            {
                "name": name,
                "location": location,
                "required": required,
                "description": description,
                "schema": OPENAPI_TYPES[param_type.__name__.lower()],
            }
        )
        return obj

    return decorator


def openapi_examples(
    request_examples: dict[str, Any] | None = None, response_examples: dict[str, Any] | None = None
) -> Decorator:
    """Request and response examples."""

    def decorator[F: Callable[..., Any]](func: F) -> F:
        if request_examples:
            _openapi(func)["examples"]["request"] = request_examples
        if response_examples:
            _openapi(func)["examples"]["response"] = response_examples
        return func

    return decorator


def openapi_deprecated(reason: str = "This endpoint is deprecated") -> Decorator:
    """Mark an endpoint as deprecated."""
    return _annotate(_openapi_deprecated=True, _openapi_deprecation_reason=reason)


def openapi_operation_id(operation_id: str) -> Decorator:
    """Set a custom operation ID for the endpoint."""
    return _annotate(_openapi_operation_id=operation_id)


def openapi_external_docs(url: str, description: str = "External documentation") -> Decorator:
    """Add external documentation reference."""
    return _annotate(_openapi_external_docs={"url": url, "description": description})


def openapi_security(security_requirements: list[dict[str, list[str]]]) -> Decorator:
    """Override security requirements for this endpoint (`[]` for a public one)."""
    return _annotate(_openapi_security=security_requirements)

from functools import wraps
from http import HTTPStatus
from typing import Type, List, Optional, Dict, Any

from pydantic import BaseModel

from apps.openapi.types import ParameterLocation, OPENAPI_TYPES, HTTP_STATUS_DESCRIPTIONS


def openapi_response(status_code: HTTPStatus, description: str, serializer: Type[BaseModel] | None = None):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        if not hasattr(func, "_openapi"):
            func._openapi = {"responses": {}, "parameters": []}

        func._openapi["responses"][status_code] = {"description": description, "serializer": serializer}

        return wrapped

    return decorator


def openapi_metadata(description: str, tags: List[str], summary: Optional[str] = None):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_description", description)
        setattr(wrapped, "_openapi_tags", tags)
        setattr(wrapped, "_openapi_summary", summary)

        return wrapped

    return decorator


def openapi_parameter(
    name: str, param_type: Type, location: ParameterLocation, description: str, required: bool = True
):
    def decorator(obj):
        if isinstance(obj, type):
            result = obj
        else:

            @wraps(obj)
            def wrapper(*args, **kwargs):
                return obj(*args, **kwargs)

            result = wrapper

        if not hasattr(result, "_openapi_parameters"):
            result._openapi_parameters = []

        result._openapi_parameters.append(
            {
                "name": name,
                "location": location,
                "required": required,
                "description": description,
                "schema": OPENAPI_TYPES[param_type.__name__.lower()],
            }
        )

        return result

    return decorator


def openapi_examples(
    request_examples: Optional[Dict[str, Any]] = None, response_examples: Optional[Dict[str, Any]] = None
):
    """Add examples to OpenAPI documentation."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        if not hasattr(func, "_openapi"):
            func._openapi = {"responses": {}, "parameters": [], "examples": {}}

        if request_examples:
            func._openapi["examples"]["request"] = request_examples
        if response_examples:
            func._openapi["examples"]["response"] = response_examples

        return wrapped

    return decorator


def openapi_deprecated(reason: str = "This endpoint is deprecated"):
    """Mark an endpoint as deprecated."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_deprecated", True)
        setattr(wrapped, "_openapi_deprecation_reason", reason)

        return wrapped

    return decorator


def openapi_operation_id(operation_id: str):
    """Set a custom operation ID for the endpoint."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_operation_id", operation_id)

        return wrapped

    return decorator


def openapi_external_docs(url: str, description: str = "External documentation"):
    """Add external documentation reference."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_external_docs", {"url": url, "description": description})

        return wrapped

    return decorator


def openapi_servers(servers: List[Dict[str, str]]):
    """Add server information to the endpoint."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_servers", servers)

        return wrapped

    return decorator


def openapi_security(security_requirements: List[Dict[str, List[str]]]):
    """Override security requirements for this endpoint."""

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        setattr(wrapped, "_openapi_security", security_requirements)

        return wrapped

    return decorator

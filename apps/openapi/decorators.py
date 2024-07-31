from functools import wraps
from http import HTTPStatus
from typing import Type, List, Optional

from pydantic import BaseModel

from apps.openapi.types import ParameterLocation, OPENAPI_TYPES


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

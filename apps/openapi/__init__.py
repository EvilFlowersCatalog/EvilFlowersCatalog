"""`from apps import openapi`: `@openapi.metadata`, `@openapi.response`, `@openapi.parameter` on view methods."""

from .decorators import openapi_metadata as metadata
from .decorators import openapi_parameter as parameter
from .decorators import openapi_response as response

__all__ = ["metadata", "parameter", "response"]

from typing import Type, Dict, List

from django.conf import settings
from django.urls import get_resolver
from django_api_forms import Form
from django_filters import FilterSet

from apps.api.serializers import Serializer
from apps.openapi.schema import OpenApiDocument
from apps.openapi.types import COMMON_SCHEMAS


class OpenApiService:
    @staticmethod
    def _find_all_subclasses(needle: Type) -> Dict[str, Type]:
        subclasses: set = set()
        to_process: List[Type] = [needle]

        while to_process:
            parent = to_process.pop()
            for child in parent.__subclasses__():
                if child not in subclasses:
                    subclasses.add(child)
                    to_process.append(child)

        return {cls.__qualname__: cls for cls in subclasses}

    def __init__(self):
        self._openapi = OpenApiDocument()

    def _process_url_patterns(self, url_patterns, prefix=""):
        """Recursively process URL patterns to handle nested includes."""
        for pattern in url_patterns:
            if hasattr(pattern, "url_patterns"):
                # This is an include() - process recursively
                app_name = getattr(pattern, "app_name", None)
                if app_name in settings.EVILFLOWERS_OPENAPI_APPS:
                    new_prefix = f"{prefix}/{pattern.pattern}"
                    self._process_url_patterns(pattern.url_patterns, new_prefix)
            elif hasattr(pattern, "lookup_str"):
                # This is a regular URL pattern
                full_path = f"{prefix}/{pattern.pattern}"
                self._openapi.add_path(full_path, pattern.lookup_str)

    def build(self):
        # Add common schemas first
        for schema_name, schema_definition in COMMON_SCHEMAS.items():
            self._openapi.components["schemas"][schema_name] = schema_definition

        # Serializers
        serializers = self._find_all_subclasses(Serializer)
        for name, serializer in serializers.items():
            self._openapi.add_serializer(name, serializer)

        # Forms
        forms = self._find_all_subclasses(Form)
        for name, form in forms.items():
            self._openapi.add_form(name, form)

        # Filters
        filters = self._find_all_subclasses(FilterSet)
        for name, django_filter in filters.items():
            self._openapi.add_filter(name, django_filter)

        # Security schemes
        self._openapi.add_security(
            "basicAuth",
            {
                "type": "http",
                "scheme": "basic",
                "description": "Basic HTTP authentication using username and password",
            },
        )
        self._openapi.add_security(
            "bearerAuth",
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT Bearer token authentication",
            },
        )

        # Process URL patterns recursively
        self._process_url_patterns(get_resolver().url_patterns)

        return self._openapi.model_dump(exclude_none=True, by_alias=True)

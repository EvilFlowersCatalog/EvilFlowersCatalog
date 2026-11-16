from django.conf import settings
from django.urls import get_resolver
from django_api_forms import Form
from django_filters import FilterSet

from apps.api.serializers import Serializer
from apps.openapi.schema import OpenApiDocument
from apps.openapi.types import COMMON_SCHEMAS


class OpenApiService:
    @staticmethod
    def _find_all_subclasses[T](needle: type[T]) -> dict[str, type[T]]:
        subclasses: set[type[T]] = set()
        to_process: list[type[T]] = [needle]

        while to_process:
            parent = to_process.pop()
            child: type[T]
            for child in parent.__subclasses__():
                if child not in subclasses:
                    subclasses.add(child)
                    to_process.append(child)

        # Sorted: a set's order changes between runs, and every reorder was a diff in evilflowers-protocol.
        ordered = sorted(subclasses, key=lambda cls: (cls.__module__, cls.__qualname__))
        return {cls.__qualname__: cls for cls in ordered}

    def __init__(self):
        self._openapi = OpenApiDocument()

    def _process_url_patterns(self, url_patterns, prefix=""):
        """Recursively process URL patterns to handle nested includes."""
        for pattern in url_patterns:
            if hasattr(pattern, "url_patterns"):
                # This is an include() - process recursively
                app_name = getattr(pattern, "app_name", None)
                if app_name in settings.EVILFLOWERS_OPENAPI_APPS:
                    self._process_url_patterns(pattern.url_patterns, self._join(prefix, str(pattern.pattern)))
            elif hasattr(pattern, "lookup_str"):
                # This is a regular URL pattern
                self._openapi.add_path(self._join(prefix, str(pattern.pattern)), pattern.lookup_str)

    @staticmethod
    def _join(prefix: str, part: str) -> str:
        """`/extraction/v1` + `runs/…`: include prefixes end with `/`, which a naive join doubled (#180).

        An empty part is the include's own root: `mcp/v1` + `""` is routed as `/mcp/v1`, not `/mcp/v1/`.
        """
        if not part:
            return prefix if prefix.startswith("/") else f"/{prefix}"
        return f"{prefix.rstrip('/')}/{part.lstrip('/')}"

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
        self._openapi.finalize()

        return self._openapi.model_dump(exclude_none=True, by_alias=True)

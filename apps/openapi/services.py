from typing import Type, Dict, List

from django.conf import settings
from django.urls import get_resolver
from django_api_forms import Form
from django_filters import FilterSet

from apps.api.serializers import Serializer
from apps.openapi.schema import OpenApiDocument


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

    def build(self):
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

        self._openapi.add_security("basicAuth", {"type": "http", "scheme": "basic"})
        self._openapi.add_security("bearerAuth", {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"})

        # FIXME: for real implementation you should do it recursively
        for router in get_resolver().url_patterns:
            if getattr(router, "app_name", None) not in settings.EVILFLOWERS_OPENAPI_APPS:
                continue

            for pattern in router.url_patterns:
                self._openapi.add_path(f"/{router.pattern}{pattern.pattern}", pattern.lookup_str)

        return self._openapi.model_dump(exclude_none=True, by_alias=True)

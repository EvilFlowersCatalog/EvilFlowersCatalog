from django.conf import settings
from django.http import JsonResponse
from django.urls import get_resolver
from django.views.generic.base import View

from apps.openapi.services import OpenApiSpecification


class OpenApiManagement(View):
    def get(self, request):
        openapi = OpenApiSpecification()

        # FIXME: for real implementation you should do it recursively
        for router in get_resolver().url_patterns:
            if getattr(router, "app_name", None) not in settings.EVILFLOWERS_OPENAPI_APPS:
                continue

            for pattern in router.url_patterns:
                openapi.add_path(f"/{router.pattern}{pattern.pattern}", pattern.lookup_str)

        return JsonResponse(data=openapi.model_dump(exclude_none=True, by_alias=True))

from django.http import JsonResponse
from django.views.generic.base import View

from apps.openapi.services import OpenApiService


class OpenApiManagement(View):
    def get(self, request):
        service = OpenApiService()

        return JsonResponse(data=service.build())

import json

from django.http import JsonResponse
from django.views import View


class ReadiumHook(View):
    def dispatch(self, request, *args, **kwargs):
        try:
            content = json.loads(request.body)
        except json.JSONDecoder:
            return JsonResponse({"number": 32})

        print(content)

        return JsonResponse({"number": 32})

"""IP-035: the console's own pages and JSON endpoints.

Conventions every feature package follows (grid, graph, vectors, jobs, modules):

* `urlpatterns(site)` returns its routes, each wrapped in `site.admin_view`, named `console_<name>`;
* a page is a `ConsolePage` subclass rendering a template that extends `console/page.html` (the admin
  chrome, breadcrumbs, theme and the vendored front-end libraries it asks for in `{% block vendor %}`);
* data is fetched by the page from a `ConsoleApi` endpoint (JSON, same session), so pages stay static
  HTML plus Alpine.js components and nothing needs a JavaScript build step;
* a page belongs to a module (`module = "jobs"`) and answers 404 while the module is off.
"""

import json
from typing import Optional

from django.http import Http404, JsonResponse
from django.views import View
from django.views.generic import TemplateView

from apps.console.modules import BY_KEY, is_enabled


class ModuleGate:
    #: Key of the module the view belongs to; `None` for views of the console itself.
    module: Optional[str] = None
    site = None

    def gate(self):
        if self.module is not None and not is_enabled(self.module):
            raise Http404("This console module is switched off.")


class ConsolePage(ModuleGate, TemplateView):
    title = ""

    def dispatch(self, request, *args, **kwargs):
        self.gate()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.site.each_context(self.request))
        context["title"] = self.get_title()
        context["console_module"] = BY_KEY.get(self.module) if self.module else None
        return context

    def get_title(self):
        return self.title


class ConsoleApi(ModuleGate, View):
    """A JSON endpoint. `get_data`/`post_data` return a JSON-serialisable value or raise `ApiError`."""

    def dispatch(self, request, *args, **kwargs):
        try:
            self.gate()
            return super().dispatch(request, *args, **kwargs)
        except ApiError as error:
            return JsonResponse({"error": error.message}, status=error.status)
        except Http404 as error:
            return JsonResponse({"error": str(error) or "Not found"}, status=404)

    def get(self, request, *args, **kwargs):
        return JsonResponse(self.get_data(request, *args, **kwargs), safe=False, json_dumps_params={"default": str})

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            raise ApiError("The request body is not JSON.")
        return JsonResponse(
            self.post_data(request, payload, *args, **kwargs), safe=False, json_dumps_params={"default": str}
        )

    def get_data(self, request, *args, **kwargs):  # pragma: no cover - overridden
        raise ApiError("Method not allowed", status=405)

    def post_data(self, request, payload, *args, **kwargs):  # pragma: no cover - overridden
        raise ApiError("Method not allowed", status=405)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))

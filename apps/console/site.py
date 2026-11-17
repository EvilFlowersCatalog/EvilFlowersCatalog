"""IP-035: the admin site of the console.

It replaces Django's default site (`ElviraAdminConfig.default_site`), so every `admin.site.register`
lands here. What it adds to the stock site:

* the index and the sidebar list **modules** (`apps.console.modules`), not Django apps;
* a page per module (`console/module/<key>/`) and the Modules page that switches them;
* per-model "needs attention" counters (`ConsoleMixin.attention`) and planner row estimates on the index;
* the interactive console pages and their JSON endpoints (`apps.console.urls`), all behind
  `admin_view`, so they share the admin's session login, staff check and CSRF protection.
"""

from typing import Dict, List, Optional

from django.contrib import admin
from django.db import connection
from django.urls import NoReverseMatch, path, reverse
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _

from apps.console import modules as console_modules


class ElviraAdminSite(admin.AdminSite):
    site_header = "ELVIRA"
    site_title = _("ELVIRA administration")
    index_title = _("Administration")
    index_template = "console/index.html"
    app_index_template = "console/app_index.html"
    enable_nav_sidebar = True

    def __init__(self, name="admin"):
        super().__init__(name)
        self._console_modules: Dict[type, str] = {}

    # --- module bookkeeping ----------------------------------------------------------------------------

    def assign_module(self, model, key: str) -> None:
        self._console_modules[model] = key

    def module_of(self, model) -> Optional[str]:
        return self._console_modules.get(model)

    def declared_models(self) -> Dict[type, str]:
        """Every registered model and its module, enabled or not (the coverage test reads this)."""
        return dict(self._console_modules)

    # --- grouping --------------------------------------------------------------------------------------

    def get_app_list(self, request, app_label=None):
        if app_label is not None:
            if app_label in console_modules.BY_KEY:
                return self.get_module_list(request, only=app_label)
            return super().get_app_list(request, app_label)
        return self.get_module_list(request)

    def get_module_list(self, request, only: Optional[str] = None) -> List[dict]:
        app_dict = self._build_app_dict(request)
        by_model: Dict[str, dict] = {}
        for app in app_dict.values():
            for entry in app["models"]:
                by_model[entry["model"]._meta.label] = entry

        estimates = self._estimates([entry["model"] for entry in by_model.values()])
        grouped: Dict[str, List[dict]] = {}
        unassigned: List[dict] = []
        for label, entry in by_model.items():
            key = self.module_of(entry["model"])
            entry["estimate"] = estimates.get(entry["model"]._meta.db_table)
            entry["grid_url"] = self._grid_url(entry["model"])
            if key is None:
                unassigned.append(entry)
            else:
                grouped.setdefault(key, []).append(entry)

        result = []
        for module in console_modules.MODULES:
            if only is not None and module.key != only:
                continue
            entries = grouped.get(module.key)
            if not entries:
                continue
            entries.sort(key=lambda item: str(item["name"]))
            result.append(
                {
                    "name": module.title,
                    "app_label": module.key,
                    "app_url": reverse(f"{self.name}:console_module", args=[module.key], current_app=self.name),
                    "has_module_perms": True,
                    "models": entries,
                    "module": module,
                }
            )
        if unassigned and only is None:
            unassigned.sort(key=lambda item: str(item["name"]))
            result.append(
                {
                    "name": _("Other"),
                    "app_label": "other",
                    "app_url": reverse(f"{self.name}:index", current_app=self.name),
                    "has_module_perms": True,
                    "models": unassigned,
                }
            )
        return result

    def _grid_url(self, model) -> Optional[str]:
        try:
            return reverse(
                f"{self.name}:console_grid",
                args=[model._meta.app_label, model._meta.model_name],
                current_app=self.name,
            )
        except NoReverseMatch:
            return None

    @staticmethod
    def _estimates(models) -> Dict[str, int]:
        """Planner row estimates for many tables in one query (`reltuples`; -1 = never analysed)."""
        tables = [model._meta.db_table for model in models]
        if not tables:
            return {}
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname, reltuples::bigint FROM pg_class "
                "WHERE relkind IN ('r', 'p') AND relname = ANY(%s) AND relnamespace = 'public'::regnamespace",
                [tables],
            )
            return {name: int(count) for name, count in cursor.fetchall()}

    # --- attention -------------------------------------------------------------------------------------

    def attention(self, request) -> List[dict]:
        """`ConsoleMixin.attention` of every visible admin: label, count and the filtered list URL."""
        items = []
        for model, model_admin in self._registry.items():
            declared = getattr(model_admin, "attention", ())
            if not declared or not model_admin.has_view_permission(request):
                continue
            for label, lookup in declared:
                count = model_admin.get_queryset(request).filter(**lookup).count()
                if not count:
                    continue
                url = reverse(
                    f"{self.name}:{model._meta.app_label}_{model._meta.model_name}_changelist",
                    current_app=self.name,
                )
                items.append(
                    {
                        "label": label,
                        "count": count,
                        "url": f"{url}?{urlencode(lookup)}",
                        "module": self.module_of(model),
                    }
                )
        return items

    # --- views -----------------------------------------------------------------------------------------

    def each_context(self, request):
        context = super().each_context(request)
        context["console_states"] = console_modules.states() if request.user.is_authenticated else {}
        return context

    def index(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "attention": self.attention(request)}
        return super().index(request, extra_context)

    def get_urls(self):
        from apps.console.urls import console_urlpatterns

        return console_urlpatterns(self) + super().get_urls()

    def module_index(self, request, key):
        from django.http import Http404

        if key not in console_modules.BY_KEY or not console_modules.is_enabled(key):
            raise Http404
        return self.app_index(request, key)

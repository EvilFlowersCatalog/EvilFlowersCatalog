"""IP-035: the Modules page, which switches console modules on and off at runtime.

The switch is the instance setting `admin.modules` (`apps.core.settings_store`): the page stores only
the modules whose state differs from the deployment default, so a later change of
`EVILFLOWERS_ADMIN_MODULES` still reaches every module nobody switched by hand.
"""

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _

from apps.console import modules as console_modules
from apps.console.views import ConsolePage


class ModulesPage(ConsolePage):
    template_name = "console/modules.html"
    title = _("Modules")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["states"] = list(console_modules.states().values())
        return context

    def post(self, request, *args, **kwargs):
        from apps.core import settings_store

        defaults = console_modules.deployment_defaults()
        overrides = {}
        for module in console_modules.MODULES:
            wanted = request.POST.get(f"module-{module.key}") == "on"
            if wanted != defaults[module.key]:
                overrides[module.key] = wanted
        if request.POST.get("reset") == "1":
            overrides = {}
        result = settings_store.update(console_modules.SETTING_KEY, overrides or None, user=request.user)
        if result["changed"]:
            messages.success(request, _("Modules saved."))
        else:
            messages.info(request, _("Nothing changed."))
        return HttpResponseRedirect(reverse(f"{self.site.name}:console_modules", current_app=self.site.name))


def urlpatterns(site):
    return [
        path("console/modules/", site.admin_view(ModulesPage.as_view(site=site)), name="console_modules"),
    ]

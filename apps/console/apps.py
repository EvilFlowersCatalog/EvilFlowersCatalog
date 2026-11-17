from django.apps import AppConfig

# Imported as a module: a bare `AdminConfig` name here would be a second default config of this module.
from django.contrib.admin import apps as admin_apps


class ConsoleConfig(AppConfig):
    """Templates, static files and the `admin.modules` setting of the console (IP-035).

    Listed before the admin in `INSTALLED_APPS`, so its `admin/…` templates override Django's.
    """

    name = "apps.console"
    label = "console"
    verbose_name = "Console"
    default = True

    def ready(self):
        from apps.console.modules import register_setting

        register_setting()


class ElviraAdminConfig(admin_apps.AdminConfig):
    """Replaces `django.contrib.admin` in `INSTALLED_APPS`: the same app, our site as the default."""

    default_site = "apps.console.site.ElviraAdminSite"
    default = False

from django.db import models
from django.utils.translation import gettext_lazy as _


class Setting(models.Model):
    """One instance-wide setting a superuser changes without a deploy (#20).

    The row holds only values someone stored; the keys, their types, defaults and
    descriptions live in the code registry (`apps.core.settings_store`), which
    validates every write. A missing row inherits the `EVILFLOWERS_*` Django
    setting named in the registry, then the built-in default. `value` is JSON with
    no size limit, so a base64 logo fits.
    """

    class Meta:
        app_label = "core"
        db_table = "settings"
        default_permissions = ()
        verbose_name = _("Setting")
        verbose_name_plural = _("Settings")

    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField()
    updated_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


__all__ = ["Setting"]

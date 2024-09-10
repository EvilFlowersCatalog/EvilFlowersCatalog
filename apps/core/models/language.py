from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.base import BaseModel


class LanguageManager(models.Manager):
    def get_by_natural_key(self, alpha2):
        return self.get(alpha2=alpha2)


class Language(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "languages"
        default_permissions = ()
        verbose_name = _("Language")
        verbose_name_plural = _("Languages")

    name = models.CharField(max_length=100)
    alpha2 = models.CharField(max_length=2, unique=True)
    alpha3 = models.CharField(max_length=3, null=True)


__all__ = ["Language"]

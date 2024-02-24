from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.user import User
from apps.core.models.catalog import Catalog
from apps.core.models.base import BaseModel


class Category(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "categories"
        default_permissions = ()
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        unique_together = (("catalog", "term"),)

    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE)
    term = models.CharField(max_length=255)
    label = models.CharField(max_length=255, null=True)
    scheme = models.CharField(max_length=255, null=True)

    def __str__(self):
        return f"{self.term} ({self.id})"


__all__ = ["Category"]

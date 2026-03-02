from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.catalog import Catalog
from apps.core.models.base import BaseModel


class Author(BaseModel):
    class Meta:
        app_label = "core"
        db_table = "authors"
        default_permissions = ()
        verbose_name = _("Author")
        verbose_name_plural = _("Authors")
        indexes = [
            models.Index(fields=["catalog_id"]),
            models.Index(fields=["name"]),
            models.Index(fields=["surname"]),
            models.Index(fields=["catalog_id", "name"]),
            models.Index(fields=["catalog_id", "surname"]),
            models.Index(fields=["catalog_id", "name", "surname"]),
        ]

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    surname = models.CharField(max_length=255)

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.surname}"

    def __str__(self):
        return f"{self.full_name} ({self.id})"


__all__ = ["Author"]

from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.catalog import Catalog
from apps.core.models.base import BaseModel


class Author(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'authors'
        default_permissions = ()
        verbose_name = _('Author')
        verbose_name_plural = _('Authors')

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    surname = models.CharField(max_length=255)


__all__ = [
    'Author'
]

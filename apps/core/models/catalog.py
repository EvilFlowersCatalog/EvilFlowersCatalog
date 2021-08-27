from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.user import User
from apps.core.models.base import BaseModel


class Catalog(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'catalogs'
        default_permissions = ()
        verbose_name = _('Catalog')
        verbose_name_plural = _('Catalogs')
        unique_together = (
            ('creator_id', 'title')
        )

    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    url_name = models.SlugField(unique=True)
    title = models.CharField(max_length=100)
    users = models.ManyToManyField(User, related_name='catalogs')
    is_public = models.BooleanField(default=False)


__all__ = [
    'Catalog'
]

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.user import User
from apps.core.models.base import BaseModel
from apps.core.models.catalog import Catalog


class Feed(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'feeds'
        default_permissions = ()
        verbose_name = _('Catalog')
        verbose_name_plural = _('Catalogs')
        unique_together = (
            ('creator_id', 'title'),
            ('creator_id', 'url_name')
        )

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name='feeds')
    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    url_name = models.SlugField()
    content = models.TextField()

    @property
    def url(self):
        return f"{settings.BASE_URL}/opds/{self.catalog.url_name}/{self.url_name}.xml"


__all__ = [
    'Feed'
]

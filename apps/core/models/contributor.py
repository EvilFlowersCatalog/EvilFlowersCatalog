from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.entry import Entry
from apps.core.models.author import Author
from apps.core.models.base import BaseModel


class Contributor(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'contributors'
        default_permissions = ()
        verbose_name = _('Contributor')
        verbose_name_plural = _('Contributors')

    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE)


__all__ = [
    'Contributor'
]

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.author import Author
from apps.core.models.language import Language
from apps.core.models.category import Category
from apps.core.models.user import User
from apps.core.models.catalog import Catalog
from apps.core.models.base import BaseModel


class Entry(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'entries'
        default_permissions = ()
        verbose_name = _('Entry')
        verbose_name_plural = _('Entries')

    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name='entries')
    author = models.ForeignKey(Author, on_delete=models.CASCADE, null=True, related_name='entries')
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='entries', null=True)
    language = models.ForeignKey(Language, on_delete=models.CASCADE, related_name='+', null=True)
    identifiers = ArrayField(models.CharField(max_length=100), null=True)
    title = models.CharField(max_length=255)
    summary = models.TextField(null=True)
    content = models.TextField(null=True)
    contributors = models.ManyToManyField(
        Author, related_name='contribution_entries', db_table='contributors', verbose_name=_('Contributor'),
    )


__all__ = [
    'Entry'
]

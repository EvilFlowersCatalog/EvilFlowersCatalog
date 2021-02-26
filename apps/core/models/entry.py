from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext as _
from django_api_forms import Form

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

    def fill(self, form: Form, creator: User) -> 'Entry':
        form.fill(self)

        if 'author' in form.cleaned_data.keys():
            author, created = Author.objects.get_or_create(
                catalog=self.catalog,
                name=form.cleaned_data['author']['name'],
                surname=form.cleaned_data['author']['surname']
            )
            self.author = author

        if 'category' in form.cleaned_data.keys():
            category, created = Category.objects.get_or_create(
                creator=creator,
                catalog=self.catalog,
                term=form.cleaned_data['category']['term']
            )
            if created:
                category.label = form.cleaned_data['category'].get('label')
                category.scheme = form.cleaned_data['category'].get('scheme')
                category.save()
            self.category = category

        return self


__all__ = [
    'Entry'
]

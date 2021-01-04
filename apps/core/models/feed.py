from enum import Enum

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_enum_choices.fields import EnumChoiceField

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
            ('creator_id', 'title')
        )

    class FeedRelation(Enum):
        NEW = 'new'
        POPULAR = 'popular'
        SUBSECTION = 'subsection'

        def __str__(self):
            if self == self.NEW:
                return "http://opds-spec.org/sort/new"
            elif self == self.POPULAR:
                return "http://opds-spec.org/sort/popular"
            return self.value

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name='feeds')
    creator = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=100)
    relation = EnumChoiceField(FeedRelation)
    content = models.TextField()


__all__ = [
    'Feed'
]

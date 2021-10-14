from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.base import BaseModel


class Language(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'languages'
        default_permissions = ()
        verbose_name = _('Language')
        verbose_name_plural = _('Languages')

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=2, unique=True)


__all__ = [
    'Language'
]

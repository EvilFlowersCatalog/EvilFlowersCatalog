from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.base import BaseModel


class Currency(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'currencies'
        default_permissions = ()
        verbose_name = _('Currency')
        verbose_name_plural = _('Currencies')

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=3, unique=True)


__all__ = [
    'Currency'
]

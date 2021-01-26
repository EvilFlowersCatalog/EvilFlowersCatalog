from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.base import BaseModel
from apps.core.models.currency import Currency
from apps.core.models.entry import Entry


class Price(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'prices'
        default_permissions = ()
        verbose_name = _('Price')
        verbose_name_plural = _('Prices')

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name='prices')
    currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name='+')
    value = models.DecimalField(max_digits=12, decimal_places=4)


__all__ = [
    'Price'
]

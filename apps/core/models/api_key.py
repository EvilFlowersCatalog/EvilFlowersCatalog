from enum import Enum

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_enum_choices.fields import EnumChoiceField

from apps.core.models.base import BaseModel


class ApiKey(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'api_keys'
        default_permissions = ()
        verbose_name = _('API key')
        verbose_name_plural = _('API keys')

    class DevicePlatform(Enum):
        WEB = 'web'
        DEBUG = 'debug'

        def __str__(self):
            return _(f"api_key({self.value})")

    name = models.CharField(max_length=200, null=True)
    platform = EnumChoiceField(DevicePlatform, null=False, default=DevicePlatform.DEBUG)
    secret = models.CharField(max_length=30, null=False)
    is_active = models.BooleanField(default=False)


__all__ = [
    'ApiKey'
]

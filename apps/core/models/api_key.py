from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import User
from apps.core.models.base import BaseModel


class ApiKey(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'api_keys'
        default_permissions = ()
        verbose_name = _('API key')
        verbose_name_plural = _('API keys')

    class DevicePlatform(models.TextChoices):
        DEBUG = 'debug', _('debug')
        CUSTOM = 'custom', _('custom')
        USER = 'user', _('user')

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=200, null=True)
    platform = models.TextField(max_length=20, choices=DevicePlatform.choices, null=False, default=DevicePlatform.USER)
    secret = models.CharField(max_length=30, null=False)
    is_active = models.BooleanField(default=False)


__all__ = [
    'ApiKey'
]

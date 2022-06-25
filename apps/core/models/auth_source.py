from django.db import models
from django.utils.translation import gettext as _

from apps.core.models.base import BaseModel


class AuthSource(BaseModel):
    class Meta:
        app_label = 'core'
        db_table = 'auth_sources'
        default_permissions = ()
        verbose_name = _('Authentication source')
        verbose_name_plural = _('Authentication sources')

    class Driver(models.TextChoices):
        DATABASE = 'database', _('database')
        LDAP = 'ldap', _('ldap')

    name = models.CharField(max_length=200)
    driver = models.CharField(max_length=20, choices=Driver.choices)
    content = models.JSONField(null=True)
    is_active = models.BooleanField(default=True)


__all__ = [
    'AuthSource'
]

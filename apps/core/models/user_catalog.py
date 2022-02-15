from django.db import models
from django.utils.translation import gettext as _


class UserCatalog(models.Model):
    class Meta:
        app_label = 'core'
        db_table = 'user_catalogs'
        default_permissions = ()
        verbose_name = _('User catalog')
        verbose_name_plural = _('User catalogs')

    class Mode(models.TextChoices):
        READ = 'read', _('Read')
        WRITE = 'write', _('Write')
        MANAGE = 'manage', _('Manage')

    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='user_catalogs')
    catalog = models.ForeignKey('Catalog', on_delete=models.CASCADE, related_name='user_catalogs')
    mode = models.CharField(max_length=10, choices=Mode.choices)

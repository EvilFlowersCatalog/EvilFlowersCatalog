from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import Entry, User
from apps.core.models.base import BaseModel


class License(BaseModel):
    class Meta:
        app_label = "readium"
        db_table = "licenses"
        default_permissions = ()
        verbose_name = _("License")
        verbose_name_plural = _("Licenses")

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    expires_at = models.DateTimeField()

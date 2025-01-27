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

    class LicenseState(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        RETURNED = "returned", _("Returned")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")
        CANCELLED = "cancelled", _("Cancelled")

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    state = models.CharField(choices=LicenseState.choices, default=LicenseState.DRAFT, max_length=15)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()

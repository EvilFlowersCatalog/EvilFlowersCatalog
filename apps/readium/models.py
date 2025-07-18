from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from datetime import timedelta

from apps.core.models import Entry, User, UserAcquisition
from apps.core.models.base import BaseModel


class License(BaseModel):
    class Meta:
        app_label = "readium"
        db_table = "licenses"
        default_permissions = ()
        verbose_name = _("License")
        verbose_name_plural = _("Licenses")
        unique_together = [["entry", "user", "state"]]

    class LicenseState(models.TextChoices):
        READY = "ready", _("Ready")
        ACTIVE = "active", _("Active")
        RETURNED = "returned", _("Returned")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")
        CANCELLED = "cancelled", _("Cancelled")

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="licenses")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="licenses")
    state = models.CharField(choices=LicenseState.choices, default=LicenseState.READY, max_length=15)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    # LCP-specific fields
    lcp_license_id = models.UUIDField(null=True, blank=True, unique=True)
    passphrase_hint = models.CharField(max_length=255, null=True, blank=True)
    passphrase_hash = models.CharField(max_length=64, null=True, blank=True)  # SHA256 hex
    content_id = models.CharField(max_length=255, null=True, blank=True)  # Reference to encrypted content
    device_count = models.PositiveIntegerField(default=0)

    @property
    def is_active(self):
        return self.state == self.LicenseState.ACTIVE

    @property
    def is_expired(self):
        from django.utils import timezone

        return timezone.now() > self.expires_at

    @property
    def can_be_activated(self):
        return self.state == self.LicenseState.READY and not self.is_expired


@receiver(post_save, sender=UserAcquisition)
def create_license_for_readium_entry(sender, instance: UserAcquisition, created: bool, **kwargs):
    """
    Automatically create a license when a user acquires a readium-enabled entry
    """
    if not created:
        return

    entry = instance.acquisition.entry

    # Only create license for readium-enabled entries
    if not entry.read_config("readium_enabled"):
        return

    # Check if user already has a license for this entry
    existing_license = License.objects.filter(
        entry=entry, user=instance.user, state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE]
    ).first()

    if existing_license:
        return  # User already has an active license

    # Check availability
    max_concurrent = entry.read_config("readium_amount")
    active_licenses = License.objects.filter(
        entry=entry, state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE]
    ).count()

    if active_licenses >= max_concurrent:
        # Could raise an exception or handle differently
        return

    # Create license with default 14-day duration
    start_date = timezone.now()
    end_date = start_date + timedelta(days=14)

    License.objects.create(
        entry=entry,
        user=instance.user,
        starts_at=start_date,
        expires_at=end_date,
        state=License.LicenseState.READY,
        content_id=str(instance.acquisition.pk),  # Reference to the acquisition
    )

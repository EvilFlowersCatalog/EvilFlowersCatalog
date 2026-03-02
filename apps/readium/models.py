from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from datetime import timedelta

from apps.core.models import Entry, User, UserAcquisition, Acquisition
from apps.core.models.base import BaseModel


class EncryptedContent(BaseModel):
    """
    Tracks the encryption status of a publication.

    One EncryptedContent per Acquisition (per-publication encryption).
    This is the Standard LCP approach - encrypt once, generate multiple licenses.
    """

    class Meta:
        app_label = "readium"
        db_table = "encrypted_contents"
        default_permissions = ()
        verbose_name = _("Encrypted Content")
        verbose_name_plural = _("Encrypted Contents")

    class EncryptionStatus(models.TextChoices):
        PENDING = "pending", _("Pending Encryption")
        ENCRYPTING = "encrypting", _("Encrypting")
        COMPLETED = "completed", _("Encryption Completed")
        FAILED = "failed", _("Encryption Failed")
        REGISTERED = "registered", _("Registered with LCP Server")

    acquisition = models.OneToOneField(Acquisition, on_delete=models.CASCADE, related_name="encrypted_content")

    # Encryption status
    status = models.CharField(max_length=20, choices=EncryptionStatus.choices, default=EncryptionStatus.PENDING)

    # LCP Server content ID (used for all licenses of this publication)
    lcp_content_id = models.CharField(max_length=255, unique=True)

    # Encrypted file path (relative to storage root)
    encrypted_path = models.CharField(max_length=500)

    # Public URL for downloading encrypted content
    encrypted_url = models.URLField(max_length=500, null=True, blank=True)

    # Encryption metadata
    encryption_algorithm = models.CharField(max_length=50, default="http://www.w3.org/2001/04/xmlenc#aes256-cbc")
    content_key_encrypted = models.TextField(null=True, blank=True)  # Encrypted content key from LCP server

    # Tracking
    encrypted_at = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)


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

    # Link to encrypted content (shared by all licenses for this entry)
    encrypted_content = models.ForeignKey(
        EncryptedContent,
        on_delete=models.PROTECT,  # Don't allow deleting encrypted content with active licenses
        related_name="licenses",
        null=True,  # Nullable during migration
        blank=True,
    )

    state = models.CharField(choices=LicenseState.choices, default=LicenseState.READY, max_length=15)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    # LCP-specific fields
    lcp_license_id = models.UUIDField(null=True, blank=True, unique=True)
    passphrase_hint = models.CharField(max_length=255, null=True, blank=True)
    passphrase_hash = models.CharField(max_length=64, null=True, blank=True)  # SHA256 hex
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


# Signal-based license creation removed - licenses are now created explicitly
# via LicenseService.create_license() from API views.
#
# Previous implementation automatically created licenses on UserAcquisition creation,
# which was problematic because:
# 1. Required passphrase is only available at license creation time
# 2. Implicit behavior made flow hard to understand and debug
# 3. No way to handle errors or validate availability properly
#
# New flow:
# 1. User requests license via POST /api/licenses/
# 2. LicenseService.create_license() validates, creates License, generates LCP license
# 3. License is explicitly managed through service layer

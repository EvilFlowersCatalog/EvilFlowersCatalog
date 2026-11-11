from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Acquisition, Entry, User
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

    # Tracking
    encrypted_at = models.DateTimeField(null=True, blank=True)
    registered_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    # Last dispatch to the lcpencrypt worker (issue #99). `task_id` is the
    # Celery task id the catalog minted for it; the worker's result report
    # carries it back, and a report for any other task id is stale.
    # `trace_id` is the trace the dispatch ran in (None with telemetry off).
    task_id = models.CharField(max_length=64, null=True, blank=True)
    trace_id = models.CharField(max_length=32, null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_dispatched_at = models.DateTimeField(null=True, blank=True)
    # The worker's result report for `task_id`: package summary on success,
    # whatever it built before failing otherwise. Never holds the content key.
    report = models.JSONField(null=True, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)


class License(BaseModel):
    class Meta:
        app_label = "readium"
        db_table = "licenses"
        default_permissions = ()
        verbose_name = _("License")
        verbose_name_plural = _("Licenses")
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "user"],
                condition=Q(state__in=["ready", "active"]),
                name="unique_active_license_per_user_per_entry",
            ),
        ]
        indexes = [
            # GitHub #131: statistics read licenses by creation time, per entry and per catalog.
            models.Index(fields=["entry", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

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
        # RESTRICT, not PROTECT: deleting only the acquisition (and with it the
        # package) is still refused while licenses point at it, but deleting the
        # whole entry is allowed — its licenses go in the same cascade. PROTECT
        # refused that too, so any publication that had ever been lent could not
        # be deleted at all (REST answered 500 with ProtectedError).
        on_delete=models.RESTRICT,
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

    # IP-009 Phase 5: per-license renewal counter. Incremented inside
    # the same transaction as the state mutation in
    # `LicenseService.renew_license`. `evaluate_renew` consults this
    # plus `EVILFLOWERS_READIUM_MAX_RENEWALS` to enforce the per-loan
    # renewal cap.
    renewal_count = models.PositiveIntegerField(default=0)

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


class Reservation(BaseModel):
    """
    A user's place in the queue for a fully-borrowed LCP-enabled entry.

    State machine:
        queued      -- waiting in line behind active loans
        available   -- a slot opened up; user has `claim_deadline` to claim
        claimed     -- user converted reservation to a license (terminal)
        expired     -- claim window passed without action (terminal)
        cancelled   -- user-cancelled or admin-cancelled (terminal)

    Transitions are driven by:
    - `POST /readium/v1/reservations` -> creates with status=queued
    - `PATCH /readium/v1/reservations/{id}` { status: "cancelled" | "claimed" }
    - Server-side promotion (queued -> available) on license terminal-state transitions
    - Server-side expiry (available -> expired) on the Celery sweep job

    The unique constraint enforces "one non-terminal reservation per (entry, user)".
    """

    class Meta:
        app_label = "readium"
        db_table = "reservations"
        default_permissions = ()
        verbose_name = _("Reservation")
        verbose_name_plural = _("Reservations")
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "user"],
                condition=Q(status__in=["queued", "available"]),
                name="uniq_active_reservation_per_user_entry",
            )
        ]
        indexes = [
            models.Index(fields=["entry", "status", "position"]),
            models.Index(fields=["status", "claim_deadline"]),
            # GitHub #131: holds placed per period. (entry, status) is covered by the first index.
            models.Index(fields=["requested_at"]),
        ]

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        AVAILABLE = "available", _("Available")
        CLAIMED = "claimed", _("Claimed")
        EXPIRED = "expired", _("Expired")
        CANCELLED = "cancelled", _("Cancelled")

    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="reservations")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reservations")
    position = models.PositiveIntegerField()
    status = models.CharField(choices=Status.choices, default=Status.QUEUED, max_length=16)
    requested_at = models.DateTimeField(default=timezone.now)
    available_at = models.DateTimeField(null=True, blank=True)
    claim_deadline = models.DateTimeField(null=True, blank=True)
    claimed_license = models.ForeignKey(
        License,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="from_reservation",
    )

    TERMINAL_STATUSES = (Status.CLAIMED, Status.EXPIRED, Status.CANCELLED)
    NON_TERMINAL_STATUSES = (Status.QUEUED, Status.AVAILABLE)

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

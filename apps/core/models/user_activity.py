from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Entry, User
from apps.core.models.base import BaseModel


class UserActivity(BaseModel):
    """
    One row of a user's personal reading history (IP-016).

    Repeated identical actions on the same entry collapse into a single row: `count` grows and
    `last_occurred_at` moves forward, while `created_at` keeps the first occurrence.
    """

    class Meta:
        app_label = "core"
        db_table = "user_activities"
        default_permissions = ()
        verbose_name = _("User activity")
        verbose_name_plural = _("User activities")
        indexes = [
            models.Index(fields=["user", "-last_occurred_at"], name="user_activities_user_last_idx"),
            models.Index(fields=["user", "entry", "-last_occurred_at"], name="user_activities_entry_last_idx"),
            models.Index(fields=["last_occurred_at"], name="user_activities_last_idx"),
        ]

    class ActivityAction(models.TextChoices):
        ENTRY_DOWNLOADED = "entry_downloaded", _("Entry downloaded")
        LICENSE_DOWNLOADED = "license_downloaded", _("License downloaded")
        SHELF_ADDED = "shelf_added", _("Added to shelf")
        SHELF_REMOVED = "shelf_removed", _("Removed from shelf")
        ACQUISITION_SHARED = "acquisition_shared", _("Acquisition shared")
        LOAN_CREATED = "loan_created", _("Loan created")
        LOAN_RENEWED = "loan_renewed", _("Loan renewed")
        LOAN_RETURNED = "loan_returned", _("Loan returned")
        LOAN_EXPIRED = "loan_expired", _("Loan expired")
        LOAN_REVOKED = "loan_revoked", _("Loan revoked")
        LOAN_CANCELLED = "loan_cancelled", _("Loan cancelled")
        RESERVATION_CREATED = "reservation_created", _("Reservation created")
        RESERVATION_AVAILABLE = "reservation_available", _("Reservation available")
        RESERVATION_CLAIMED = "reservation_claimed", _("Reservation claimed")
        RESERVATION_EXPIRED = "reservation_expired", _("Reservation expired")
        RESERVATION_CANCELLED = "reservation_cancelled", _("Reservation cancelled")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="activities")
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="activities")
    action = models.CharField(max_length=32, choices=ActivityAction.choices)
    count = models.PositiveIntegerField(default=1)
    last_occurred_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict)

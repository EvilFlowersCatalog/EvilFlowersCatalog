from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class NotificationLog(BaseModel):
    """Tracks every notification attempt."""

    class Meta:
        app_label = "notifications"
        db_table = "notification_logs"
        default_permissions = ()
        verbose_name = _("Notification Log")
        verbose_name_plural = _("Notification Logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "notification_type"]),
            models.Index(fields=["status", "created_at"]),
        ]

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        NO_CONTACT = "no_contact", _("No Contact")

    class NotificationType(models.TextChoices):
        LICENSE_CREATED = "license_created", _("License Created")

    recipient = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    recipient_email = models.EmailField(blank=True, default="")
    notification_type = models.CharField(max_length=50, choices=NotificationType.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    subject = models.CharField(max_length=255, blank=True, default="")
    context_snapshot = models.JSONField(default=dict)
    error_message = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.notification_type} -> {self.recipient_email} ({self.status})"

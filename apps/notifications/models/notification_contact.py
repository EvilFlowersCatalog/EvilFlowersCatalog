from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class NotificationContact(BaseModel):
    """Maps users to their notification contact addresses."""

    class Meta:
        app_label = "notifications"
        db_table = "notification_contacts"
        default_permissions = ()
        verbose_name = _("Notification Contact")
        verbose_name_plural = _("Notification Contacts")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "type", "value"],
                name="unique_user_contact",
            ),
        ]

    class ContactType(models.TextChoices):
        EMAIL = "email", _("Email")

    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="notification_contacts")
    type = models.CharField(max_length=20, choices=ContactType.choices)
    value = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user} ({self.type}: {self.value})"

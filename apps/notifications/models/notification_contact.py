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

    class Source(models.TextChoices):
        #: Entered by the user (REST API / portal) or an operator.
        USER = "user", _("User")
        #: Imported from an LDAP auth source (`NOTIFICATION_CONTACT_MAP`).
        #: The importer owns these rows: it rewrites them when the directory
        #: changes, but never demotes a primary contact the user chose (IP-025).
        LDAP = "ldap", _("LDAP")

    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="notification_contacts")
    type = models.CharField(max_length=20, choices=ContactType.choices)
    value = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.USER)

    def __str__(self):
        return f"{self.user} ({self.type}: {self.value})"

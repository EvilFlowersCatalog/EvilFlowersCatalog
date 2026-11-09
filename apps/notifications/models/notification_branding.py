from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class NotificationBranding(BaseModel):
    """Deployment-wide look of notification e-mails, edited by a superuser over MCP (IP-025).

    One row at most. Every field is optional: an empty value means "inherit"
    — from settings (`EVILFLOWERS_NOTIFICATION_LIBRARY_NAME`, …) and then the
    built-in defaults in `apps.notifications.branding.DEFAULTS`. The row holds
    *tokens* (name, colours, logo, footer), never template source; structural
    changes to an e-mail are template overrides on the IaC-mounted volume
    (`EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR`).
    """

    class Meta:
        app_label = "notifications"
        db_table = "notification_branding"
        default_permissions = ()
        verbose_name = _("Notification branding")
        verbose_name_plural = _("Notification branding")

    library_name = models.CharField(max_length=200, blank=True)
    #: An asset name from `<overrides dir>/assets/` (served by the catalog) or an absolute https URL.
    logo = models.CharField(max_length=500, blank=True)
    logo_alt = models.CharField(max_length=200, blank=True)
    logo_width = models.PositiveSmallIntegerField(null=True, blank=True)
    logo_link = models.URLField(max_length=500, blank=True)
    header_background = models.CharField(max_length=7, blank=True)
    header_text_color = models.CharField(max_length=7, blank=True)
    primary_color = models.CharField(max_length=7, blank=True)
    button_text_color = models.CharField(max_length=7, blank=True)
    background_color = models.CharField(max_length=7, blank=True)
    footer_text = models.TextField(blank=True)
    website_url = models.URLField(max_length=500, blank=True)
    from_name = models.CharField(max_length=100, blank=True)
    updated_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    TOKEN_FIELDS = (
        "library_name",
        "logo",
        "logo_alt",
        "logo_width",
        "logo_link",
        "header_background",
        "header_text_color",
        "primary_color",
        "button_text_color",
        "background_color",
        "footer_text",
        "website_url",
        "from_name",
    )

    @classmethod
    def current(cls) -> "NotificationBranding | None":
        return cls.objects.order_by("created_at").first()

    def __str__(self):
        return self.library_name or "Notification branding"

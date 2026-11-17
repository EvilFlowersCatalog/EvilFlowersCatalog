"""Notifications in the console (IP-035): branding and contacts editable, the delivery log read-only."""

from django.utils.translation import gettext_lazy as _

from apps.console.options import ConsoleModelAdmin, register
from apps.notifications.models import NotificationBranding, NotificationContact, NotificationLog


@register(NotificationBranding, module="notifications")
class NotificationBrandingAdmin(ConsoleModelAdmin):
    editable = True
    list_display = ("library_name", "logo", "primary_color", "updated_by", "updated_at")
    readonly_fields = ("updated_by",)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@register(NotificationContact, module="notifications")
class NotificationContactAdmin(ConsoleModelAdmin):
    editable = True
    deletable = True
    list_display = ("user", "type", "value", "is_primary", "source", "created_at")
    list_filter = ("type", "is_primary", "source")
    search_fields = ("value", "user__username", "user__name", "user__surname")


@register(NotificationLog, module="notifications")
class NotificationLogAdmin(ConsoleModelAdmin):
    list_display = ("notification_type", "recipient_email", "status", "subject", "created_at", "sent_at")
    list_filter = ("notification_type", "status")
    search_fields = ("recipient_email", "subject")
    date_hierarchy = "created_at"
    attention = ((_("notifications failed"), {"status": NotificationLog.Status.FAILED}),)

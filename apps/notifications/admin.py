from django.contrib import admin

from apps.notifications.models import NotificationBranding, NotificationContact, NotificationLog


@admin.register(NotificationBranding)
class NotificationBrandingAdmin(admin.ModelAdmin):
    list_display = ("library_name", "logo", "primary_color", "updated_by", "updated_at")
    raw_id_fields = ("updated_by",)


@admin.register(NotificationContact)
class NotificationContactAdmin(admin.ModelAdmin):
    list_display = ("user", "type", "value", "is_primary", "source", "created_at")
    list_filter = ("type", "is_primary", "source")
    search_fields = ("value", "user__username", "user__name", "user__surname")
    raw_id_fields = ("user",)


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("notification_type", "recipient_email", "status", "subject", "created_at", "sent_at")
    list_filter = ("notification_type", "status")
    search_fields = ("recipient_email", "subject")
    raw_id_fields = ("recipient",)
    readonly_fields = ("context_snapshot", "error_message", "sent_at")
    date_hierarchy = "created_at"

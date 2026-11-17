"""IP-035: the admin's own audit log, in the System module (read-only)."""

from django.contrib.admin.models import LogEntry

from apps.console.options import ConsoleModelAdmin, register


@register(LogEntry, module="system")
class LogEntryAdmin(ConsoleModelAdmin):
    list_display = ("action_time", "user", "content_type", "object_repr", "action_flag", "change_message")
    list_filter = ("action_flag", "content_type")
    search_fields = ("object_repr", "change_message", "user__username")
    date_hierarchy = "action_time"
    ordering = ("-action_time",)

from django.contrib import admin

from apps.assistant.models import AssistantUserPolicy, Chat, ChatMessage, SectionSummary


@admin.register(SectionSummary)
class SectionSummaryAdmin(admin.ModelAdmin):
    """Generated section summaries (IP-030 §4), for reading them against their sections."""

    list_display = ("entry", "first_page", "last_page", "section", "model", "updated_at")
    search_fields = ("section", "entry__title", "summary")
    raw_id_fields = ("entry", "run")
    readonly_fields = ("digest", "model", "tokens_used", "concepts")


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "message_count", "total_tokens", "last_message_at")
    list_filter = ("is_active",)
    search_fields = ("title", "user__username")
    raw_id_fields = ("user", "entry", "catalog")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "role", "tokens_used", "model_seconds", "error", "prompt_fingerprint", "created_at")
    list_filter = ("role", "error", "prompt_fingerprint")
    raw_id_fields = ("chat", "user")


@admin.register(AssistantUserPolicy)
class AssistantUserPolicyAdmin(admin.ModelAdmin):
    list_display = ("user", "is_blocked", "blocked_until", "daily_message_limit", "daily_seconds_limit")
    list_filter = ("is_blocked",)
    search_fields = ("user__username",)
    raw_id_fields = ("user",)

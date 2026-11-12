from django.contrib import admin

from apps.assistant.models import AssistantUserPolicy, ChapterSummary, Chat, ChatMessage


@admin.register(ChapterSummary)
class ChapterSummaryAdmin(admin.ModelAdmin):
    """Generated chapter summaries (#119), for reading them against their chapters."""

    list_display = ("entry", "page", "end_page", "title", "model", "updated_at")
    search_fields = ("title", "entry__title", "summary")
    raw_id_fields = ("entry",)
    readonly_fields = ("digest", "model", "tokens_used")


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "message_count", "total_tokens", "last_message_at")
    list_filter = ("is_active",)
    search_fields = ("title", "user__username")
    raw_id_fields = ("user", "entry", "catalog")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "role", "tokens_used", "prompt_fingerprint", "created_at")
    list_filter = ("role", "prompt_fingerprint")
    raw_id_fields = ("chat", "user")


@admin.register(AssistantUserPolicy)
class AssistantUserPolicyAdmin(admin.ModelAdmin):
    list_display = ("user", "is_blocked", "blocked_until", "daily_message_limit", "daily_token_limit")
    list_filter = ("is_blocked",)
    search_fields = ("user__username",)
    raw_id_fields = ("user",)

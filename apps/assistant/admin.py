from django.contrib import admin

from apps.assistant.models import AssistantUserPolicy, Chat, ChatMessage


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "message_count", "total_tokens", "last_message_at")
    list_filter = ("is_active",)
    search_fields = ("title", "user__username")
    raw_id_fields = ("user", "entry", "catalog")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "chat", "role", "tokens_used", "created_at")
    list_filter = ("role",)
    raw_id_fields = ("chat", "user")


@admin.register(AssistantUserPolicy)
class AssistantUserPolicyAdmin(admin.ModelAdmin):
    list_display = ("user", "is_blocked", "blocked_until", "daily_message_limit", "daily_token_limit")
    list_filter = ("is_blocked",)
    search_fields = ("user__username",)
    raw_id_fields = ("user",)

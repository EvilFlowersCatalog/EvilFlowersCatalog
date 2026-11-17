"""The assistant in the console (IP-035): summaries and limits in Assistant, transcripts in the
sensitive Assistant conversations module (off by default)."""

from apps.assistant.models import AssistantUserPolicy, Chat, ChatMessage, SectionSummary
from apps.console.options import ConsoleModelAdmin, register


@register(SectionSummary, module="assistant")
class SectionSummaryAdmin(ConsoleModelAdmin):
    """Generated section summaries (IP-030 §4), for reading them against their sections."""

    list_display = ("entry", "first_page", "last_page", "section", "model", "updated_at")
    search_fields = ("section", "entry__title", "summary")
    readonly_fields = ("digest", "model", "tokens_used", "concepts")


@register(AssistantUserPolicy, module="assistant")
class AssistantUserPolicyAdmin(ConsoleModelAdmin):
    """Block a reader or change their daily limits."""

    editable = True
    deletable = True
    list_display = ("user", "is_blocked", "blocked_until", "daily_message_limit", "daily_seconds_limit")
    list_filter = ("is_blocked",)
    search_fields = ("user__username",)


@register(Chat, module="conversations")
class ChatAdmin(ConsoleModelAdmin):
    list_display = ("id", "user", "title", "message_count", "total_tokens", "last_message_at")
    list_filter = ("is_active",)
    search_fields = ("title", "user__username")


@register(ChatMessage, module="conversations")
class ChatMessageAdmin(ConsoleModelAdmin):
    list_display = ("id", "chat", "role", "tokens_used", "model_seconds", "error", "prompt_fingerprint", "created_at")
    list_filter = ("role", "error", "prompt_fingerprint")

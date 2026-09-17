from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class ChatMessage(BaseModel):
    """One turn in a conversation.

    IP-015 D8: tool activity is stored structurally. Because turns are
    stateless, whatever is persisted here *is* the conversation replayed to the
    model on the next turn — so tool calls and their results are columns, not
    prose written into `text`.
    """

    class Meta:
        app_label = "assistant"
        db_table = "assistant_messages"
        default_permissions = ()
        verbose_name = _("Chat message")
        verbose_name_plural = _("Chat messages")
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["chat", "created_at"]),
            # Daily usage is derived from these rows instead of a counter table
            # (D2), so the quota check depends on this index.
            models.Index(fields=["user", "created_at"]),
        ]

    class Role(models.TextChoices):
        USER = "user", _("User")
        ASSISTANT = "assistant", _("Assistant")
        TOOL = "tool", _("Tool")

    chat = models.ForeignKey("assistant.Chat", on_delete=models.CASCADE, related_name="messages")

    #: Denormalised from `chat.user` so the quota query does not have to join.
    #: SET_NULL keeps a conversation readable after a user is removed.
    user = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_messages"
    )

    role = models.CharField(max_length=10, choices=Role.choices)
    text = models.TextField(blank=True, default="")

    #: Tool calls the model asked for on this turn, as issued. Empty for plain
    #: user and assistant messages.
    tool_calls = models.JSONField(null=True, blank=True)

    #: The result handed back for a `role=tool` message.
    tool_result = models.JSONField(null=True, blank=True)

    #: Publications the assistant asked the UI to render, in display order.
    #: This is the persisted form of the `displayBooks` tool.
    displayed_entries = models.JSONField(null=True, blank=True)

    tokens_used = models.IntegerField(default=0)

    def save(self, *args, **kwargs):
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating:
            self._touch_chat()

    def _touch_chat(self) -> None:
        """Maintain the parent chat's counters and title.

        The reference implementation did this with two PostgreSQL triggers. It
        lives here instead so it is visible to anyone reading the model and can
        be exercised in tests without a database fixture.
        """
        from apps.assistant.models.chat import Chat

        updates = {
            "message_count": models.F("message_count") + 1,
            "total_tokens": models.F("total_tokens") + (self.tokens_used or 0),
            "last_message_at": timezone.now(),
        }

        # The first thing the user says names the conversation.
        if self.role == self.Role.USER and self.text:
            Chat.objects.filter(pk=self.chat_id, title="").update(title=self.text[:100])

        Chat.objects.filter(pk=self.chat_id).update(**updates)

    def __str__(self):
        return f"{self.role}: {self.text[:50]}"

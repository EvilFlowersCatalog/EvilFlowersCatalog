from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


def visible_q() -> models.Q:
    """Rows a reader sees: what they said and what the assistant answered.

    Tool results and the assistant turns that only request tools are kept for
    replaying the conversation to the model, but they are plumbing, not
    conversation, and never leave the server.
    """
    return models.Q(role="user") | (
        models.Q(role="assistant")
        & models.Q(tool_calls__isnull=True)
        & (~models.Q(text="") | models.Q(displayed_entries__isnull=False))
    )


class ChatMessageQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(visible_q())


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
    #: This is the persisted form of the `displayBooks` tool, set on the answer
    #: that closes the turn in which the tool was called.
    displayed_entries = models.JSONField(null=True, blank=True)

    tokens_used = models.IntegerField(default=0)

    objects = ChatMessageQuerySet.as_manager()

    @property
    def is_visible(self) -> bool:
        """Python twin of `visible_q()`."""
        if self.role == self.Role.USER:
            return True
        if self.role == self.Role.ASSISTANT:
            return self.tool_calls is None and (bool(self.text) or self.displayed_entries is not None)
        return False

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
            "total_tokens": models.F("total_tokens") + (self.tokens_used or 0),
            "last_message_at": timezone.now(),
        }
        # Only what the reader sees is counted; tool traffic still costs tokens.
        if self.is_visible:
            updates["message_count"] = models.F("message_count") + 1

        # The first thing the user says names the conversation.
        if self.role == self.Role.USER and self.text:
            Chat.objects.filter(pk=self.chat_id, title="").update(title=self.text[:100])

        Chat.objects.filter(pk=self.chat_id).update(**updates)

    def __str__(self):
        return f"{self.role}: {self.text[:50]}"

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models.base import BaseModel


class Chat(BaseModel):
    """One conversation between a user and the assistant.

    IP-015 D2: chats live in the catalog database and reference `core.User`
    directly. The reference implementation mirrored the catalog's users into a
    table of its own because it ran as a separate service; nothing here does.
    """

    class Meta:
        app_label = "assistant"
        db_table = "assistant_chats"
        default_permissions = ()
        verbose_name = _("Chat")
        verbose_name_plural = _("Chats")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            # The context policy (D3) reads `last_message_at` on every turn to
            # decide whether the conversation is still warm.
            models.Index(fields=["user", "-last_message_at"]),
        ]

    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="assistant_chats")

    #: Set when the conversation was opened from a specific publication
    #: ("ask about this book"). SET_NULL so removing a publication does not
    #: destroy the conversation that referenced it.
    entry = models.ForeignKey(
        "core.Entry", on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_chats"
    )

    #: IP-015 D4: there is a single catalog in practice, so nothing branches on
    #: this. It is accepted from the client and stored for compatibility and
    #: for later reporting only.
    catalog = models.ForeignKey(
        "core.Catalog", on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_chats"
    )

    title = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)

    #: Maintained by `ChatMessage` on insert rather than by a database trigger,
    #: so the behaviour is visible in Python and testable.
    total_tokens = models.BigIntegerField(default=0)
    message_count = models.IntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.title or str(self.pk)

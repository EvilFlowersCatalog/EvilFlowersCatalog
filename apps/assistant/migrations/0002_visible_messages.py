"""Move displayed publications onto the answers and recount visible messages.

Before this, `displayBooks` stored its ids on the `role=tool` row, and
`message_count` counted every row including tool traffic. The ids now live on
the assistant answer that closes the turn, and only visible messages count.
"""

from django.db import migrations
from django.db.models import Q


def _visible_q() -> Q:
    # Frozen copy of `apps.assistant.models.chat_message.visible_q`.
    return Q(role="user") | (
        Q(role="assistant") & Q(tool_calls__isnull=True) & (~Q(text="") | Q(displayed_entries__isnull=False))
    )


def forwards(apps, schema_editor):
    Chat = apps.get_model("assistant", "Chat")
    ChatMessage = apps.get_model("assistant", "ChatMessage")

    for chat in Chat.objects.all().iterator():
        pending = []

        for message in ChatMessage.objects.filter(chat=chat).order_by("created_at"):
            if message.role == "user":
                # A turn that never produced an answer keeps nothing to attach to.
                pending = []
            elif message.role == "tool" and message.displayed_entries:
                pending.extend(e for e in message.displayed_entries if e not in pending)
                message.displayed_entries = None
                message.save(update_fields=["displayed_entries"])
            elif message.role == "assistant" and message.tool_calls is None and pending:
                message.displayed_entries = list(pending)
                message.save(update_fields=["displayed_entries"])
                pending = []

        chat.message_count = ChatMessage.objects.filter(chat=chat).filter(_visible_q()).count()
        chat.save(update_fields=["message_count"])


class Migration(migrations.Migration):
    dependencies = [
        ("assistant", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

"""Quota, moderation and conversation-context policy."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.assistant.models import Chat, ChatMessage
from apps.assistant.tools import DISPLAY_BOOKS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Elvíra, the assistant of a digital publication library. You help readers find "
    "publications in this library and answer questions about them.\n\n"
    "Use `search_entries` to find publications. It filters by title, author, category, language, "
    "publication date and borrowing availability — use the `lcp_states` filter when the reader "
    "asks what they can borrow right now. Use `get_entry` for details about one publication.\n\n"
    f"Whenever you mention specific publications, call `{DISPLAY_BOOKS}` with their ids so the "
    "reader sees them as cards, and keep your own reply brief instead of repeating titles and "
    "authors in prose.\n\n"
    "You cannot borrow, reserve or return anything on the reader's behalf — they do that "
    "themselves in the library interface. If asked about something unrelated to the library, "
    "look for publications on that topic and say that searching the library is what you are for."
)


class QuotaExceeded(Exception):
    def __init__(self, limit: int, used: int, reset_at):
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        super().__init__("Daily assistant limit exceeded")


class UserBlocked(Exception):
    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__("User is blocked from the assistant")


@dataclass
class Usage:
    messages: int
    tokens: int
    message_limit: int
    token_limit: int

    @property
    def exceeded(self) -> bool:
        return self.messages >= self.message_limit or self.tokens >= self.token_limit


def _policy(user):
    """The user's assistant policy, or `None` when they have the defaults."""
    return getattr(user, "assistant_policy", None)


def assert_not_blocked(user) -> None:
    policy = _policy(user)
    if policy is not None and policy.blocked:
        raise UserBlocked(policy.blocked_reason)


def day_start() -> "timezone.datetime":
    """Start of the current quota day, honouring the configured reset hour."""
    now = timezone.localtime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return start


def usage(user) -> Usage:
    """Today's consumption, derived from the message rows themselves.

    IP-015 D2: there is no counter table. The tokens are already recorded on
    every message, so a second tally would only be a second source of truth
    that can drift out of step with the conversation it describes.
    """
    policy = _policy(user)

    rows = ChatMessage.objects.filter(user=user, created_at__gte=day_start())
    aggregate = rows.aggregate(tokens=Sum("tokens_used"))

    return Usage(
        messages=rows.filter(role=ChatMessage.Role.USER).count(),
        tokens=aggregate["tokens"] or 0,
        message_limit=(
            policy.daily_message_limit
            if policy is not None and policy.daily_message_limit is not None
            else settings.EVILFLOWERS_ASSISTANT_DAILY_LIMIT_MESSAGES
        ),
        token_limit=(
            policy.daily_token_limit
            if policy is not None and policy.daily_token_limit is not None
            else settings.EVILFLOWERS_ASSISTANT_DAILY_LIMIT_TOKENS
        ),
    )


def assert_within_quota(user) -> Usage:
    current = usage(user)
    if current.exceeded:
        raise QuotaExceeded(
            limit=current.message_limit,
            used=current.messages,
            reset_at=day_start() + timedelta(days=1),
        )
    return current


def _history_queryset(chat: Chat):
    return chat.messages.order_by("created_at")


def conversation(chat: Chat) -> List[dict]:
    """Rebuild the message array to send to the model.

    IP-015 D3. While a conversation is warm the entire history is replayed
    unchanged, so the prompt prefix is byte-identical to last turn's and the
    backend can reuse its KV cache. Once the last message is older than the
    configured TTL that cache is gone anyway, so the tail is replayed instead
    and becomes the new stable prefix.

    Nothing volatile — no timestamps, no counters — may be added to the prefix,
    or the cache is invalidated on every turn.
    """
    messages: List[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    rows = _history_queryset(chat)

    if _is_cold(chat):
        keep = settings.EVILFLOWERS_ASSISTANT_TRIM_MESSAGES
        # Take the newest `keep` rows, then restore chronological order.
        rows = list(reversed(list(rows.reverse()[:keep])))

    for row in rows:
        messages.append(_as_message(row))

    return messages


def _is_cold(chat: Chat) -> bool:
    if chat.last_message_at is None:
        return False
    age = (timezone.now() - chat.last_message_at).total_seconds()
    return age > settings.EVILFLOWERS_ASSISTANT_CACHE_TTL


def _as_message(row: ChatMessage) -> dict:
    if row.role == ChatMessage.Role.TOOL:
        return {"role": "tool", "content": row.text}

    message: dict = {"role": row.role, "content": row.text}
    if row.tool_calls:
        message["tool_calls"] = row.tool_calls
    return message


def opening_context(chat: Chat) -> Optional[str]:
    """A note pinning the conversation to a publication, when it started from one.

    Added once, as the first user-visible turn, so it stays part of the stable
    prefix rather than being re-injected on every request.
    """
    if chat.entry_id is None:
        return None
    return f"The reader is asking about the publication with id {chat.entry_id}."

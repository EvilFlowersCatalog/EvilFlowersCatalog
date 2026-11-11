"""Quota, moderation and conversation-context policy."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from django.utils.html import strip_tags

from apps.assistant import contexts, prompts
from apps.assistant.models import Chat, ChatMessage
from apps.assistant.tools import DISPLAY_BOOKS, FIND_SIMILAR, READ_PAGE, SEARCH_PASSAGES

logger = logging.getLogger(__name__)

#: IP-022. Sections in a fixed order: role, tool policy, grounding, language,
#: limits, style. Nothing per request goes in here — it is the head of the
#: cached prefix (IP-015 D3); the reader's language is pinned by the last
#: message of every request instead (`language_instruction`). The text is
#: `prompts/chat.md` (#116).
SYSTEM_PROMPT = prompts.render(
    "chat",
    search_passages=SEARCH_PASSAGES,
    find_similar=FIND_SIMILAR,
    display_books=DISPLAY_BOOKS,
    read_page=READ_PAGE,
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
    """Start of the current quota day: midnight in EVILFLOWERS_DISPLAY_TIME_ZONE.

    The processes run in UTC (#107); the reader's day starts at their midnight.
    """
    now = timezone.localtime(timezone=ZoneInfo(settings.EVILFLOWERS_DISPLAY_TIME_ZONE))
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
    # `entry` is read for every user message that carries one (see `_as_message`).
    return chat.messages.select_related("entry").order_by("created_at")


def conversation(chat: Chat, document=None) -> List[dict]:
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

    opening = opening_context(chat, document)
    if opening:
        messages.append({"role": "system", "content": opening})

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


#: How much of a publication's summary is inlined into the note about it.
ENTRY_NOTE_SUMMARY_CHARS = 500


def entry_note(entry) -> str:
    """Tells the model which publication the reader attached.

    Title and a short plain-text summary are inlined so the model can answer
    straight away; `get_entry` stays available when it needs more.
    """
    summary = " ".join(strip_tags(entry.summary or "").split())
    if len(summary) > ENTRY_NOTE_SUMMARY_CHARS:
        summary = summary[:ENTRY_NOTE_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"

    note = f'The reader is attaching the publication {entry.pk}: "{entry.title}"'
    if summary:
        note += f" — {summary}"
    note += "\nAnswer from this when it is enough; use the get_entry tool for more detail."
    if entry.read_config("readium_enabled"):
        # Lent through Readium LCP: its text is never given to the model (IP-018 Q5 follow-up).
        note += (
            "\nThis publication is lent through Readium LCP, so you cannot read its text. If the reader asks "
            "about its content, say so and tell them to borrow it in the library (the button on the "
            'publication is labelled "Vypožičať" in Slovak and "Borrow" in English) and read it in Thorium; '
            "you can still answer from its catalogue description."
        )
    return note


def _as_message(row: ChatMessage) -> dict:
    if row.role == ChatMessage.Role.TOOL:
        return {"role": "tool", "content": row.text}

    text = row.text
    if row.role == ChatMessage.Role.USER:
        # Derived from the stored rows only, so the replayed prefix stays identical.
        notes = reader_notes(row)
        if notes:
            text = f"[{notes}]\n\n{text}"

    message: dict = {"role": row.role, "content": text}
    if row.tool_calls:
        message["tool_calls"] = row.tool_calls
    return message


def reader_notes(row: ChatMessage) -> str:
    """What came with a user message: the attached publication, the page and the viewer contexts."""
    notes = []
    if row.entry is not None:
        notes.append(entry_note(row.entry))
    if row.page:
        notes.append(f"The reader is on page {row.page} of the viewer.")
    if row.contexts:
        notes.append("The reader attached:\n" + "\n".join(f"- {contexts.describe(c)}" for c in row.contexts))
    return "\n".join(notes)


LANGUAGE_NAMES = prompts.LANGUAGE_NAMES


def language_instruction(language: Optional[str]) -> Optional[str]:
    """Pins the answer to the reader's UI language.

    Sent as the last message of every request, after the history, so it never
    changes the cached prefix. Small models drift (Slovak into Czech, a DRM
    notice into Russian), so the language is named and Czech is ruled out.
    """
    name = prompts.language_name(language)
    if name is None:
        return None
    return (
        f"Write your answer in {name}, whatever language the reader's message, the publication or a tool "
        "result is in." + prompts.not_czech(language)
    )


def start_agent_annotation(user_acquisition, prompt: str, language: Optional[str] = None):
    """A pending agent annotation of the reader's copy, its highlight task queued on commit (IP-018)."""
    from django.db import transaction

    from apps.assistant import tasks
    from apps.core.models import Annotation

    prompt = prompt.strip()
    annotation = Annotation.objects.create(
        user_acquisition=user_acquisition,
        kind=Annotation.Kind.AGENT,
        status=Annotation.Status.PENDING,
        title=prompt if len(prompt) <= 80 else prompt[:79] + "…",
        prompt=prompt,
    )
    transaction.on_commit(lambda: tasks.highlight_annotation.delay(str(annotation.pk), language))
    return annotation


def reading_note(document) -> str:
    """Tells the model it can read the publication's PDF and how to cite it."""
    return (
        f"You can read this publication's PDF, numbered as the reader's viewer numbers it. It has "
        f"{document.page_count} pages. "
        "Use `search_paragraphs` to find passages by meaning, `search_pdf` for an exact term (with "
        "`regex: true` for a regular expression, e.g. every 'Table 2.x') and "
        "`read_pdf_page` to read pages; `list_regions` lists every figure, table and equation, which you "
        "cite as [[region:ID]]. When the reader asks you to mark, highlight or find passages for them, "
        "call `create_highlights` with what to look for; the highlights appear in their viewer. Base "
        "answers about the publication on what it says and never invent its content. Answer in your own "
        "words and briefly: never paste the text of a page back to the reader, quote at most a sentence.\n"
        "Cite the page of everything you take from the publication as [[page:N]] with the viewer's page "
        "number, for example [[page:12]], right after the sentence it supports; the reader clicks it to "
        "jump there. A figure, table or equation can be "
        "cited as [[region:ID]] with its id. Never use page numbers printed in the book itself; they differ "
        "from the viewer's."
    )


def opening_context(chat: Chat, document=None) -> Optional[str]:
    """A note pinning the conversation to a publication, when it started from one.

    Added once, right after the system prompt, so it stays part of the stable
    prefix rather than being re-injected on every request. `document` is the
    reader's copy when the chat is bound to one; its page count does not change,
    so the note stays stable too.
    """
    notes = []
    if chat.entry is not None:
        notes.append(entry_note(chat.entry))
    if document is not None:
        notes.append(reading_note(document))
    return "\n\n".join(notes) or None

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
from apps.assistant.tools import (
    DISPLAY_BOOKS,
    FIND_SIMILAR,
    FOLLOW,
    LIBRARY_OVERVIEW,
    READ_PAGE,
    SEARCH_PASSAGES,
    STUDY,
)

logger = logging.getLogger(__name__)

#: IP-030 §7. Sections in a fixed order: role and capabilities, evidence and tools, grounding and citations,
#: answer shape, limits. Nothing per request goes in here — it is the head of the cached prefix (IP-015 D3);
#: the reader's language is pinned after each of their messages instead (`language_instruction`, Q9: the
#: prompt has no language rule of its own). The text is `prompts/chat.md` (#116).
SYSTEM_PROMPT = prompts.render(
    "chat",
    search_passages=SEARCH_PASSAGES,
    find_similar=FIND_SIMILAR,
    display_books=DISPLAY_BOOKS,
    read_page=READ_PAGE,
    follow=FOLLOW,
    study=STUDY,
    library_overview=LIBRARY_OVERVIEW,
)


class QuotaExceeded(Exception):
    def __init__(self, limit: int, used: int, reset_at, unit: str = "messages"):
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        #: `messages` or `seconds`: which limit was reached.
        self.unit = unit
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
    #: Model seconds today (IP-030 Q2) and the limit; `tokens` stays for telemetry only.
    seconds: float = 0.0
    seconds_limit: int = 0

    @property
    def exceeded(self) -> bool:
        return self.messages >= self.message_limit or (self.seconds_limit > 0 and self.seconds >= self.seconds_limit)

    @property
    def unit(self) -> str:
        return "messages" if self.messages >= self.message_limit else "seconds"


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
    aggregate = rows.aggregate(tokens=Sum("tokens_used"), seconds=Sum("model_seconds"))

    return Usage(
        messages=rows.filter(role=ChatMessage.Role.USER).count(),
        tokens=aggregate["tokens"] or 0,
        message_limit=(
            policy.daily_message_limit
            if policy is not None and policy.daily_message_limit is not None
            else settings.EVILFLOWERS_ASSISTANT_DAILY_LIMIT_MESSAGES
        ),
        seconds=round(aggregate["seconds"] or 0.0, 1),
        seconds_limit=(
            policy.daily_seconds_limit
            if policy is not None and policy.daily_seconds_limit is not None
            else settings.EVILFLOWERS_ASSISTANT_DAILY_LIMIT_SECONDS
        ),
    )


def assert_within_quota(user) -> Usage:
    current = usage(user)
    if current.exceeded:
        unit = current.unit
        raise QuotaExceeded(
            limit=current.message_limit if unit == "messages" else current.seconds_limit,
            used=current.messages if unit == "messages" else int(current.seconds),
            reset_at=day_start() + timedelta(days=1),
            unit=unit,
        )
    return current


#: Cache key set while a chat turn runs, so off-peak model work (`tasks.describe_figures`) yields to readers.
ACTIVE_KEY = "evilflowers:assistant:turn-active"
#: Seconds the marker outlives the last round that refreshed it.
ACTIVE_SECONDS = 120


def mark_active() -> None:
    from django.core.cache import cache

    try:
        cache.set(ACTIVE_KEY, True, ACTIVE_SECONDS)
    except Exception:
        logger.debug("Could not mark a chat turn active", exc_info=True)


def chat_active() -> bool:
    from django.core.cache import cache

    try:
        return bool(cache.get(ACTIVE_KEY))
    except Exception:
        return False


def _history_queryset(chat: Chat):
    # `entry` is read for every user message that carries one (see `_as_message`).
    return chat.messages.select_related("entry").order_by("created_at")


def conversation(chat: Chat, document=None, language: Optional[str] = None) -> List[dict]:
    """Rebuild the message array to send to the model.

    IP-015 D3. Every request of a conversation must extend the previous one:
    the previous request's messages, unchanged and in order, then only new ones.
    The model is hybrid (Gated DeltaNet), so the backend cannot cut its cache at
    any token; it restores a checkpoint taken at the end of an earlier prompt,
    and only when that whole prompt is a prefix of the new one (#138).

    - History is replayed from `chat.history_start`, which `start_turn` moves
      only at the start of a turn, so the cut never slides between rounds.
    - The language pin follows every user message rather than closing the
      request: a trailing message is displaced by the next round's assistant and
      tool messages, and the prompt stops being a prefix of the next one.

    Nothing volatile — no timestamps, no counters — may be added to the prefix,
    or the cache is invalidated on every turn.
    """
    messages: List[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    opening = opening_context(chat, document)
    if opening:
        messages.append({"role": "system", "content": opening})

    rows = _history_queryset(chat)
    if chat.history_start is not None:
        rows = rows.filter(created_at__gte=chat.history_start)
    # A failed turn's notice was never the model's answer (IP-030 §5): it is not replayed.
    rows = rows.filter(error__isnull=True)

    instruction = language_instruction(language)
    for row in rows:
        messages.append(_as_message(row))
        if instruction and row.role == ChatMessage.Role.USER:
            messages.append({"role": "system", "content": instruction})

    return messages


def start_turn(chat: Chat, was_cold: bool) -> None:
    """Fix where this turn's history starts, once, before its first round (#138).

    A cold conversation's cache is gone anyway, so the history is cut to about
    the newest `EVILFLOWERS_ASSISTANT_TRIM_MESSAGES` rows, at a user message so no
    tool result is replayed without the call that asked for it. The cut is
    stored and the following warm turns replay from it, so it becomes the new
    stable prefix. `was_cold` is `is_cold(chat)` read before this turn's user
    message was stored.
    """
    mark_active()
    if not was_cold:
        return
    keep = settings.EVILFLOWERS_ASSISTANT_TRIM_MESSAGES
    rows = _history_queryset(chat)
    if chat.history_start is not None:
        rows = rows.filter(created_at__gte=chat.history_start)
    newest = list(rows.reverse().values_list("created_at", flat=True)[:keep])
    if not newest:
        return
    oldest_kept = newest[-1]
    start = (
        rows.filter(role=ChatMessage.Role.USER, created_at__gte=oldest_kept)
        .order_by("created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if start is None or start == chat.history_start:
        return
    chat.history_start = start
    Chat.objects.filter(pk=chat.pk).update(history_start=start)


def shared_prefix(previous: Optional[List[dict]], messages: List[dict]) -> int:
    """How many leading messages `messages` shares with `previous` (#138 diagnostics).

    Equal to `len(previous)` when the new request extends the previous one, which
    is what lets the backend restore its prompt-cache checkpoint.
    """
    if not previous:
        return 0
    shared = 0
    for before, now in zip(previous, messages):
        if before != now:
            break
        shared += 1
    return shared


def is_cold(chat: Chat) -> bool:
    """Whether the conversation's last message is older than the backend's cache TTL."""
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


def language_instruction(language: Optional[str]) -> str:
    """Pins the answer's language (IP-030 Q9): the reader's message, else their UI language.

    Follows every user message in the history, so it never changes the cached prefix (#138). An English
    question in the Slovak UI is answered in English; "ok" in the Slovak UI, in Slovak. Small models drift
    (Slovak into Czech, a DRM notice into Russian), so the fallback is named and Czech is always ruled out.
    """
    name = prompts.language_name(language)
    fallback = f"; if it is too short to tell, in {name}" if name else ""
    return (
        f"Answer in the language of the reader's last message{fallback}, whatever language the publication or a "
        "tool result is in. Slovak must be Slovak, never Czech; a message in Czech is answered in Slovak."
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
    # IP-030 Q1: one citation convention in every chat. `[[page:N]]` and `[[region:…]]` are no longer asked for.
    return (
        f"You can read this publication's PDF, numbered as the reader's viewer numbers it. It has "
        f"{document.page_count} pages. "
        "Use `search_paragraphs` to find passages by meaning and `read_pdf_page` to read pages; `list_regions` "
        "lists every figure, table and equation, and `follow` goes from a passage to the next one, to the "
        "figure it refers to or to the rest of its section. When the reader asks you to mark, highlight or find "
        "passages for them, call `create_highlights` with what to look for; the highlights appear in their "
        "viewer. Base answers about the publication on what it says and never invent its content. Answer in "
        "your own words and briefly: never paste the text of a page back to the reader, quote at most a "
        "sentence.\n"
        "Results carry labels: E# for a passage or page, F# for a figure, table or equation. Cite them as [E3] "
        "or [F2] right after the sentence they support; the reader clicks one to land on the paragraph or see "
        "the figure. Never use page numbers printed in the book itself; they differ from the viewer's."
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

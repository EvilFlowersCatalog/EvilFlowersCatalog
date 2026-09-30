"""One conversational turn: model, tools, model again, until it answers.

The loop is deliberately small. The model may call tools, we run them, feed the
results back and ask again, up to a bounded number of rounds so a confused model
cannot spin forever.

Everything this yields is a Server-Sent Event for the browser, and everything it
persists is a row — with tool calls and results in columns rather than prose,
because with stateless turns whatever is stored *is* the conversation replayed
next time (IP-015 D8).
"""

import json
import logging
from typing import Generator, Iterator, List

from django.utils.translation import gettext as _

from apps.assistant import services, tools
from apps.assistant.llm import Delta, LLMUnavailable, OllamaClient
from apps.assistant.models import Chat, ChatMessage

logger = logging.getLogger(__name__)

#: How many times the model may call tools before it must produce an answer.
#: search → refine → get_entry → display is already four, so leave headroom.
MAX_TOOL_ROUNDS = 6


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def run(request, chat: Chat, text: str, entry=None) -> Iterator[str]:
    """Drive one turn, yielding SSE frames.

    The generator body runs while the response streams, so anything raised here
    surfaces as an `error` frame rather than an HTTP status — the status line is
    long gone by then. `entry` is the publication the reader attached to this
    message, if any.
    """
    user = request.user
    client = OllamaClient()

    ChatMessage.objects.create(chat=chat, user=user, role=ChatMessage.Role.USER, text=text, entry=entry)

    try:
        for frame in _rounds(request, chat, client):
            yield frame
    except LLMUnavailable as error:
        logger.warning("Assistant backend unavailable: %s", error)
        yield sse("error", {"error": _("The assistant is temporarily unavailable.")})
    except Exception:
        logger.exception("Assistant turn failed")
        yield sse("error", {"error": _("The assistant failed to answer.")})
    finally:
        yield sse("done", {"chat_id": str(chat.pk)})


def _rounds(request, chat: Chat, client: OllamaClient) -> Iterator[str]:
    specifications = tools.specifications()

    # Publications shown during this turn. They belong to the answer that closes
    # it, which is the message a reader sees when the chat is read back.
    displayed: List[str] = []

    for _round in range(MAX_TOOL_ROUNDS):
        messages = services.conversation(chat)

        content, tool_calls, tokens = yield from _stream(client, messages, specifications)

        if not tool_calls:
            ChatMessage.objects.create(
                chat=chat,
                user=chat.user,
                role=ChatMessage.Role.ASSISTANT,
                text=content,
                displayed_entries=displayed or None,
                tokens_used=tokens,
            )
            return

        # Showing cards is the answer, not a lookup: when displayBooks is all the
        # model asked for, its text and the cards close the turn. Asking it again
        # only made it call more tools until the rounds ran out.
        only_display = all(
            tools.is_display_books((call.get("function") or {}).get("name") or "") for call in tool_calls
        )

        # The assistant turn that requested the tools has to be recorded before
        # their results, or the replayed conversation would show results with
        # nothing asking for them. When it closes the turn, its text moves to the
        # closing answer below so the reader sees it when the chat is read back.
        ChatMessage.objects.create(
            chat=chat,
            user=chat.user,
            role=ChatMessage.Role.ASSISTANT,
            text="" if only_display else content,
            tool_calls=tool_calls,
            tokens_used=tokens,
        )

        for call in tool_calls:
            for entry_id in (yield from _run_tool(request, chat, call)):
                if entry_id not in displayed:
                    displayed.append(entry_id)

        if only_display:
            ChatMessage.objects.create(
                chat=chat,
                user=chat.user,
                role=ChatMessage.Role.ASSISTANT,
                text=content,
                displayed_entries=displayed or None,
            )
            return

    if displayed:
        # Out of rounds, but publications were already shown — that is an answer.
        ChatMessage.objects.create(
            chat=chat,
            user=chat.user,
            role=ChatMessage.Role.ASSISTANT,
            displayed_entries=displayed,
        )
        return

    # Out of rounds with nothing to show: say so rather than leaving the reader with silence.
    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.ASSISTANT,
        text=str(_("I could not complete that search. Could you rephrase it?")),
    )
    yield sse("message", {"text": str(_("I could not complete that search. Could you rephrase it?"))})


def _stream(client: OllamaClient, messages: List[dict], specifications: List[dict]):
    """Consume one completion, forwarding text chunks as they arrive."""
    content = ""
    tool_calls: List[dict] = []
    tokens = 0

    for delta in client.chat(messages, tools=specifications):
        if delta.content:
            content += delta.content
            yield sse("chunk", {"text": delta.content})
        if delta.tool_calls:
            tool_calls.extend(delta.tool_calls)
        if delta.done:
            tokens = delta.total_tokens

    if content:
        yield sse("message", {"text": content})

    return content, tool_calls, tokens


def _run_tool(request, chat: Chat, call: dict) -> Generator[str, None, List[str]]:
    """Run one tool call, yielding SSE frames; returns publication ids to display."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    arguments = function.get("arguments") or {}

    # Ollama returns arguments as an object, but a model can still emit a JSON
    # string; accept both rather than failing the turn over formatting.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}

    if tools.is_display_books(name):
        entry_ids = [str(entry_id) for entry_id in (arguments.get("entry_ids") or [])]
        # The tool row answers the call for the model's replay; the ids
        # themselves are persisted on the turn's final answer (see `_rounds`).
        ChatMessage.objects.create(
            chat=chat,
            user=chat.user,
            role=ChatMessage.Role.TOOL,
        )
        yield sse("entries", {"entry_ids": entry_ids})
        return entry_ids

    try:
        payload = tools.call(request, name, arguments)
        text = json.dumps(payload, default=str)
    except (tools.UnknownTool, tools.ToolNotPermitted) as error:
        # Hand the refusal to the model verbatim so it can correct itself
        # instead of failing the whole turn.
        text = json.dumps({"error": str(error)})
        payload = None
    except Exception:
        logger.exception("Assistant tool '%s' raised", name)
        text = json.dumps({"error": "The tool failed."})
        payload = None

    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.TOOL,
        text=text,
        tool_result=payload,
    )
    return []

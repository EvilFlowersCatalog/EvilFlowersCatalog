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
from typing import Generator, Iterator, List, Optional

from django.conf import settings
from django.utils.translation import gettext as _

from apps.assistant import activity, evidence, explain, prompts, services, tools
from apps.assistant.llm import Delta, LLMUnavailable, OllamaClient
from apps.assistant.models import Chat, ChatMessage

logger = logging.getLogger(__name__)

#: Runaway guard only. Tool calls are not what is limited (IP-018 Q5); reading
#: the reader's copy is, by `EVILFLOWERS_ASSISTANT_MAX_PAGES_READ`, and this sits
#: well above what a turn needs so it only stops a model that loops.
MAX_TOOL_ROUNDS = 25

#: Model rounds of a chat not bound to a reader's copy (#130). The last one offers no tools, so the model has
#: to answer from what it found. A library question needs search, maybe a refinement or a page read, cards and
#: the answer; on CPU, where the prompt cache does not hit (#130 A), every round re-evaluates the whole
#: conversation, and one turn looped for 11 rounds and 696 s before this cap.
MAX_CHAT_ROUNDS = 6

#: The last message of a chat's final round: no tools on offer, answer now.
ANSWER_NOW = (
    "Answer the reader now from the tool results above; no more tools. If they hold nothing that fits, say so "
    "briefly and suggest other words."
)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def run(
    request, chat: Chat, text: str, entry=None, contexts=None, page=None, document=None, language=None
) -> Iterator[str]:
    """Drive one turn, yielding SSE frames.

    The generator body runs while the response streams, so anything raised here
    surfaces as an `error` frame rather than an HTTP status — the status line is
    long gone by then. `entry` is the publication the reader attached to this
    message, if any; `contexts` and `page` are what they attached and where they
    were in the viewer. `document` is the reader's copy when the chat is bound to
    one, which lets the model read its pages.
    """
    user = request.user
    client = OllamaClient()
    # Read before this turn's own message touches `last_message_at`.
    was_cold = services.is_cold(chat)

    ChatMessage.objects.create(
        chat=chat,
        user=user,
        role=ChatMessage.Role.USER,
        text=text,
        entry=entry,
        contexts=contexts or None,
        page=page,
    )

    try:
        services.start_turn(chat, was_cold)
        for frame in _rounds(request, chat, client, document, language):
            yield frame
    except LLMUnavailable as error:
        logger.warning("Assistant backend unavailable: %s", error)
        yield sse("error", {"error": _("The assistant is temporarily unavailable.")})
    except Exception:
        logger.exception("Assistant turn failed")
        yield sse("error", {"error": _("The assistant failed to answer.")})
    finally:
        yield sse("done", {"chat_id": str(chat.pk)})


def _rounds(request, chat: Chat, client: OllamaClient, document=None, language=None) -> Iterator[str]:
    # Fixed for the session (IP-022): the tool list is part of the cached prompt prefix.
    specifications = tools.specifications(authenticated=request.user.is_authenticated, bound=document is not None)
    if document is not None:
        specifications = specifications + explain.TOOL_SPECIFICATIONS
        if chat.user_acquisition_id and chat.user_acquisition.user_id == chat.user_id:
            # Agent annotations belong to the reader's own copy, never a shared one.
            specifications = specifications + [CREATE_HIGHLIGHTS_SPECIFICATION]

    # Publications shown and pages read during this turn. They belong to the
    # answer that closes it, which is the message a reader sees when the chat is
    # read back.
    displayed: List[str] = []
    pages_read: List[int] = []
    annotations: List[str] = []
    # Tool calls made in this turn, by (name, arguments): a repeat is answered with a hint.
    seen_calls: set = set()
    # One page budget for the turn, over the chat's own copy and any publication read by id (#127).
    reading = tools.Reading(pages_read)
    # Evidence labels issued in this chat so far; this turn's passages continue the numbering (IP-024).
    ledger = evidence.Ledger.for_chat(chat)
    # What the model is told before the conversation, as a short hash on every assistant row (#116).
    fingerprint = prompts.fingerprint(services.SYSTEM_PROMPT, specifications)

    def close(text: str = "", tokens: int = 0, keep_empty: bool = False) -> Iterator[str]:
        yield from _close(chat, ledger, text, displayed, pages_read, annotations, tokens, keep_empty, fingerprint)

    rounds = MAX_TOOL_ROUNDS if document is not None else MAX_CHAT_ROUNDS
    previous: Optional[List[dict]] = None
    for _round in range(rounds):
        # A plain chat's last round offers no tools: the model answers from what it has (#130). It is the one
        # round that does not extend the previous request (no tools, a closing message), so it re-evaluates.
        final = document is None and _round == rounds - 1
        # The language pin is part of the history, after each user message: see `services.conversation`.
        messages = services.conversation(chat, document, language)
        # #138: how much of the previous round's request this one repeats; equal to `previous_messages` when it
        # extends it, which is what the backend's prompt-cache checkpoint needs.
        prefix = {
            "evilflowers.assistant.round": _round + 1,
            "evilflowers.assistant.prompt.messages": len(messages),
            "evilflowers.assistant.prompt.previous_messages": len(previous or []),
            "evilflowers.assistant.prompt.shared_messages": services.shared_prefix(previous, messages),
        }
        previous = list(messages)
        if final:
            messages.append({"role": "system", "content": ANSWER_NOW})

        # On CPU inference a round can spend tens of seconds evaluating the prompt
        # before its first token (#104): tell the client something is happening.
        yield sse("status", {"state": "thinking", "round": _round + 1})
        content, tool_calls, tokens = yield from _stream(
            client, messages, None if final else specifications, span_attributes=prefix
        )
        if final:
            # No tools were offered; whatever came back is the answer.
            tool_calls = []
            if not content.strip() and not displayed:
                content = str(_("I could not complete that search. Could you rephrase it?"))
                yield sse("message", {"text": content})

        if not tool_calls:
            yield from close(content, tokens)
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
            prompt_fingerprint=fingerprint,
        )

        for call in tool_calls:
            run = _run_tool(
                request, chat, call, document, pages_read, annotations, language, seen_calls, ledger, reading
            )
            for entry_id in (yield from run):
                if entry_id not in displayed:
                    displayed.append(entry_id)

        if only_display:
            yield from close(content)
            return

    if displayed or annotations:
        # Out of rounds, but publications were already shown — that is an answer.
        yield from close(keep_empty=True)
        return

    # Out of rounds with nothing to show: say so rather than leaving the reader with silence.
    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.ASSISTANT,
        text=str(_("I could not complete that search. Could you rephrase it?")),
        prompt_fingerprint=fingerprint,
    )
    yield sse("message", {"text": str(_("I could not complete that search. Could you rephrase it?"))})


CREATE_HIGHLIGHTS = "create_highlights"

CREATE_HIGHLIGHTS_SPECIFICATION = {
    "type": "function",
    "function": {
        "name": CREATE_HIGHLIGHTS,
        "description": (
            "Mark the passages of this publication that answer a request, as a new layer of AI highlights in "
            "the reader's viewer. Use it when the reader asks you to highlight, mark or find passages for "
            "them. It runs in the background: tell the reader the highlights are being prepared and will "
            "appear in the viewer; do not wait for them and do not list them yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What to find, as the reader would ask it, e.g. 'definitions of entropy'.",
                }
            },
            "required": ["prompt"],
        },
    },
}

#: Agent annotations one turn may create; a model that loops is stopped here.
MAX_ANNOTATIONS_PER_TURN = 3


def _offered(chat: Chat, document) -> List[dict]:
    if document is None or not chat.user_acquisition_id or chat.user_acquisition.user_id != chat.user_id:
        return []
    return [CREATE_HIGHLIGHTS_SPECIFICATION]


def _create_highlights(chat: Chat, arguments: dict, annotations: List[str], language: Optional[str]):
    """Queue an agent annotation of the chat's copy; yields its `annotation` frame, returns the tool text."""
    from apps.assistant.serializers import AgentAnnotationSerializer

    prompt = str(arguments.get("prompt") or "").strip()[:500]
    if not prompt:
        return json.dumps({"error": "`prompt` is required."})
    if len(annotations) >= MAX_ANNOTATIONS_PER_TURN:
        return json.dumps({"error": "Enough highlight layers were created in this answer."})

    annotation = services.start_agent_annotation(chat.user_acquisition, prompt, language)
    annotations.append(str(annotation.pk))
    yield sse("annotation", AgentAnnotationSerializer.Base.build(annotation).model_dump(mode="json"))
    return json.dumps(
        {
            "annotation_id": str(annotation.pk),
            "status": "pending",
            "note": "The highlights are being prepared and will appear in the reader's viewer.",
        }
    )


def _close(
    chat: Chat,
    ledger: "evidence.Ledger",
    text: str,
    displayed: List[str],
    pages_read: List[int],
    annotations: List[str],
    tokens: int = 0,
    keep_empty: bool = False,
    fingerprint: str = "",
) -> Iterator[str]:
    """Store the answer that closes the turn, with what it showed, read and cited.

    The evidence labels in `text` are resolved against the chat's ledger (IP-024). The text is stored as it was
    streamed: an unknown label is reported, not removed, so the stored answer is the one the reader saw.
    """
    citations, unknown = ledger.cite(text)
    if unknown:
        logger.warning("Assistant answer cites unknown evidence labels %s (chat %s)", unknown, chat.pk)
    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.ASSISTANT,
        text=text,
        # An empty list (rather than None) keeps a textless answer visible (`visible_q`).
        displayed_entries=displayed if keep_empty else (displayed or None),
        pages_read=pages_read or None,
        annotations=annotations or None,
        citations=citations or None,
        tokens_used=tokens,
        prompt_fingerprint=fingerprint,
    )
    yield from _pages_frame(pages_read)
    if citations or unknown:
        yield sse("citations", {"citations": citations, "unknown": unknown})


def _pages_frame(pages_read: List[int]) -> Iterator[str]:
    """Tells the client which pages the answer was drawn from."""
    if pages_read:
        yield sse("pages", {"pages": pages_read})


def _stream(client: OllamaClient, messages: List[dict], specifications: List[dict], span_attributes=None):
    """Consume one completion, forwarding text chunks as they arrive."""
    content = ""
    tool_calls: List[dict] = []
    tokens = 0

    for delta in client.chat(messages, tools=specifications, span_attributes=span_attributes):
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


REPEATED_CALL = json.dumps(
    {
        "error": "You already called this tool with these arguments in this answer; its result is above. "
        "Answer from it, or call it with different arguments."
    }
)


def _arguments(arguments) -> dict:
    """Ollama returns arguments as an object, but a model can still emit a JSON
    string; accept both rather than failing the turn over formatting."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def _repeated(seen_calls: Optional[set], name: str, arguments: dict) -> bool:
    """Whether this call was already made in this turn; records it otherwise.

    Small models loop on a call whose result disappointed them (#104 saw it with
    Qwen3.6 in long tool sessions). Running it again changes nothing; saying so does.
    """
    if seen_calls is None:
        return False
    key = (name, json.dumps(tools.clean_arguments(arguments), sort_keys=True, default=str))
    if key in seen_calls:
        return True
    seen_calls.add(key)
    return False


def _another_publication(document, arguments: dict) -> bool:
    """A bound chat's `read_pdf_page` naming a different publication by `entry_id` (#127).

    The model learns ids from search results, and the tool has the same name in both kinds of chat. Such a call
    goes to `tools.run`, which applies the open-access rules to that publication; the reader's copy is never
    read for it.
    """
    entry_id = arguments.get("entry_id")
    if not entry_id:
        return False
    bound = getattr(getattr(document, "acquisition", None), "entry_id", None)
    return str(entry_id) != str(bound)


def _run_tool(
    request,
    chat: Chat,
    call: dict,
    document=None,
    pages_read: Optional[List[int]] = None,
    annotations: Optional[List[str]] = None,
    language: Optional[str] = None,
    seen_calls: Optional[set] = None,
    ledger: Optional["evidence.Ledger"] = None,
    reading: Optional["tools.Reading"] = None,
) -> Generator[str, None, List[str]]:
    """Run one tool call, yielding SSE frames; returns publication ids to display."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    arguments = _arguments(function.get("arguments"))

    if not tools.is_display_books(name):
        if _repeated(seen_calls, name, arguments):
            ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=REPEATED_CALL)
            return []
        yield sse("status", {"state": "tool", "tool": name, **activity.describe(name, arguments)})

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

    if (
        document is not None
        and name == CREATE_HIGHLIGHTS
        and CREATE_HIGHLIGHTS_SPECIFICATION in _offered(chat, document)
    ):
        text = yield from _create_highlights(chat, arguments, annotations if annotations is not None else [], language)
        ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=text)
        yield sse("activity", activity.summarize(name, arguments, text=text))
        return []

    if document is not None and name in explain.DOCUMENT_TOOLS and not _another_publication(document, arguments):
        # The reader's own copy, read in-process exactly as explain reads it.
        pages = pages_read if pages_read is not None else []
        # Pages read from other publications this turn count against the same budget (#127).
        budget = None
        if reading is not None:
            budget = max(settings.EVILFLOWERS_ASSISTANT_MAX_PAGES_READ - sum(map(len, reading.books.values())), 0)
        text = explain.run_document_tool(document, call, pages, budget)
        ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=text)
        yield sse("activity", activity.summarize(name, arguments, text=text))
        return []

    try:
        # The model sees a slim result; the full payload is kept on the row (IP-022).
        payload, text = tools.run(request, name, arguments, ledger=ledger, reading=reading)
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
    yield sse("activity", activity.summarize(name, arguments, payload, text))
    return []

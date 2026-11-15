"""One conversational turn: evidence, model, tools, model again, until it answers.

The loop is deliberately small. The model may call tools, we run them, feed the
results back and ask again, up to a bounded number of rounds so a confused model
cannot spin forever.

Everything this yields is a Server-Sent Event for the browser, and everything it
persists is a row — with tool calls and results in columns rather than prose,
because with stateless turns whatever is stored *is* the conversation replayed
next time (IP-015 D8).

IP-030 adds, in the order a turn meets them:

- **Evidence first** (§1, `EVILFLOWERS_ASSISTANT_PREFETCH`): before round 1 the turn gathers a pack of
  publications, passages and figures and stores it as a `gather_evidence` call the model "already made"; the
  `evidence` frame fills the portal's panel before the first token.
- **Streamed citations** (§2): `citations.Resolver` reads the chunks, holds back a half-written `[E` and sends a
  `citation` frame the first time each label appears. The `citations` frame after `message` stays the
  authoritative list.
- **A capped final round that keeps the prefix** (§5, Phase 0): the last round offers the same tools as every
  other, so its request extends the previous one; the tool results of the round before it carry the "answer now"
  instruction, and a tool call in the final round is answered with that refusal and stored, so the next turn
  replays exactly what was sent.
- **Failed turns are stored** (§5): an assistant row with `error` set and a notice, so a chat read back shows
  the failure, and the `error` frame carries its id and `retryable`.
- **A reader who leaves stops the model**: closing this generator closes the model request (`llm`), and nothing
  is yielded after that.
- **llama-server slots** (§5, Q11): the client leases one slot for the turn and restores the chat's saved state
  into it; at the end it saves and releases (`begin_turn` / `end_turn`).
- **Study artefacts and suggestions** (§6, § API): a `study` frame closes the turn like cards do, and
  `suggestions` comes before `done`.

Live testing on 2026-11-15 added:

- **Look before answering** (#162): until a content question's turn has called a tool, its round is held back;
  an answer from memory is discarded unsent, the pack is gathered as a stored `gather_evidence` call, and the
  model is asked again (once). A label the answer may not cite (`Ledger.citable`) is taken out as it streams.
- **Real cards only** (#163): `displayBooks` shows publications whose ids came from this chat's tool results.
- **An answer after the refusal** (#164): when the last round's calls are refused, one more request answers.
- **The portal's guide** (#167): `show_in_portal` sends a `navigate` frame, like cards close the turn.
"""

import hashlib
import json
import logging
import re
from typing import Generator, Iterator, List, Optional

from django.conf import settings
from django.utils.translation import gettext as _

from apps.assistant import activity, citations, document_tools, evidence, portal, prompts, services, tools
from apps.assistant import suggestions as suggestion_chips
from apps.assistant.library import ToolContext
from apps.assistant.llm import Delta, LLMUnavailable
from apps.assistant.llm import client as make_client
from apps.assistant.models import Chat, ChatMessage

logger = logging.getLogger(__name__)

#: Runaway guard only. Tool calls are not what is limited (IP-018 Q5); reading
#: the reader's copy is, by `EVILFLOWERS_ASSISTANT_MAX_PAGES_READ`, and this sits
#: well above what a turn needs so it only stops a model that loops.
MAX_TOOL_ROUNDS = 25

#: Model rounds of a chat not bound to a reader's copy (#130). In the last one the model has to answer from what
#: it found. One turn looped for 11 rounds and 696 s before this cap.
MAX_CHAT_ROUNDS = 6

#: What the round before the last tells the model, inside its tool results, and what a tool call in the last
#: round is answered with (IP-030 §5).
ANSWER_NOW = (
    "This was the last tool call of this answer. Answer the reader now from the results above; no more tools. "
    "If they hold nothing that fits, say so briefly and suggest other words."
)
REFUSED = json.dumps({"error": ANSWER_NOW})


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def run(
    request,
    chat: Chat,
    text: str,
    entry=None,
    contexts=None,
    page=None,
    document=None,
    language=None,
    portal_path: Optional[str] = None,
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
    client = make_client()
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
        portal_path=portal.clean_path(portal_path),
    )

    cancelled = False
    state = _Turn()
    # A selection, explanation or figure attached in the viewer is the evidence the question is about (#162).
    state.attached = bool(contexts)
    try:
        services.start_turn(chat, was_cold)
        begin = getattr(client, "begin_turn", None)
        if begin is not None:
            begin(str(chat.pk))
        for frame in _rounds(request, chat, client, text, entry, document, language, state):
            yield frame
    except GeneratorExit:
        # The reader closed the stream: the model request is closed with this generator; nothing more is sent.
        cancelled = True
        logger.info("Assistant turn cancelled by the client (chat %s)", chat.pk)
        raise
    except LLMUnavailable as error:
        logger.warning("Assistant backend unavailable: %s", error)
        frame = _failed(chat, "unavailable", _("The assistant is temporarily unavailable."), state)
        try:
            yield frame
        except GeneratorExit:
            # The reader left while the error frame was on its way: nothing more may be yielded.
            cancelled = True
            raise
    except Exception:
        logger.exception("Assistant turn failed")
        frame = _failed(chat, "failed", _("The assistant failed to answer."), state)
        try:
            yield frame
        except GeneratorExit:
            cancelled = True
            raise
    finally:
        end = getattr(client, "end_turn", None)
        if end is not None:
            try:
                end()
            except Exception:
                logger.exception("Ending the model turn failed (chat %s)", chat.pk)
        if not cancelled:
            done = {"chat_id": str(chat.pk)}
            if state.message_id is not None:
                # The stored answer, for feedback without re-reading the chat (#145).
                done["message_id"] = str(state.message_id)
            yield sse("done", done)


def _failed(chat: Chat, code: str, notice, state: Optional["_Turn"] = None) -> str:
    """Store the failure as the turn's answer (never replayed to the model) and say so to the client."""
    row = ChatMessage.objects.create(
        chat=chat, user=chat.user, role=ChatMessage.Role.ASSISTANT, text=str(notice), error=code
    )
    if state is not None:
        state.message_id = row.pk
    return sse("error", {"error": str(notice), "message_id": str(row.pk), "retryable": True})


def _specifications(request, chat: Chat, document) -> List[dict]:
    """Fixed for the session (IP-022): the tool list is part of the cached prompt prefix."""
    specifications = tools.specifications(
        authenticated=request.user.is_authenticated, bound=document is not None, staff=portal.is_staff(request.user)
    )
    if document is not None:
        specifications = specifications + document_tools.SPECIFICATIONS
        if chat.user_acquisition_id and chat.user_acquisition.user_id == chat.user_id:
            # Agent annotations belong to the reader's own copy, never a shared one.
            specifications = specifications + [CREATE_HIGHLIGHTS_SPECIFICATION]
    return specifications


def history_hashes(messages: List[dict]) -> List[str]:
    """A short hash per message (#138, IP-030 Phase 0): where two requests of a chat start to differ."""
    return [
        hashlib.sha256(json.dumps(message, sort_keys=True, default=str).encode()).hexdigest()[:8]
        for message in messages
    ]


class _Turn:
    """What one turn accumulates for the answer that closes it."""

    def __init__(self):
        # Publications shown and pages read during this turn. They belong to the answer that closes it, which is
        # the message a reader sees when the chat is read back.
        self.displayed: List[str] = []
        self.pages_read: List[int] = []
        self.annotations: List[str] = []
        self.study: Optional[dict] = None
        # Tool calls made in this turn, by (name, arguments): a repeat is answered with a hint.
        self.seen_calls: set = set()
        # Labels already sent as `citation` frames in this turn.
        self.cited: set = set()
        # Labels taken out of the answer: unknown, or from an earlier turn once this one has evidence (#162).
        self.dropped: List[str] = []
        # A lookup ran in this turn (any tool but the portal directives), or the pack was gathered (#162).
        self.retrieved = False
        # The model called a tool in this turn, a portal directive included: it is not answering from memory.
        self.called = False
        # The reader attached viewer contexts to the message: they are its evidence.
        self.attached = False
        # The turn already made the model look before answering once (#162); never twice.
        self.forced = False
        # The stored answer that closed the turn; `done` carries it (#145).
        self.message_id = None


def _rounds(
    request, chat: Chat, client, text: str, entry=None, document=None, language=None, state: Optional[_Turn] = None
) -> Iterator[str]:
    specifications = _specifications(request, chat, document)
    state = state if state is not None else _Turn()
    # One page budget for the turn, over the chat's own copy and any publication read by id (#127).
    reading = tools.Reading(state.pages_read)
    # Evidence labels issued in this chat so far; this turn's continue the numbering (IP-024).
    ledger = evidence.Ledger.for_chat(chat)
    context = ToolContext(
        request=request, chat=chat, document=document, ledger=ledger, reading=reading, language=language
    )
    # What the model is told before the conversation, as a short hash on every assistant row (#116).
    fingerprint = prompts.fingerprint(services.SYSTEM_PROMPT, specifications)

    def close(text: str = "", tokens: int = 0, seconds: float = 0.0, keep_empty: bool = False) -> Iterator[str]:
        yield from _close(request, chat, ledger, text, state, tokens, seconds, keep_empty, fingerprint, language)

    if settings.EVILFLOWERS_ASSISTANT_PREFETCH:
        yield from _prefetch(request, chat, context, state, text, entry, fingerprint)

    from apps.assistant.pack import needs_pack

    # A question about content must be answered from evidence looked up for it (#162).
    content_question = needs_pack(text)
    staff = portal.is_staff(request.user)
    rounds = MAX_TOOL_ROUNDS if document is not None else MAX_CHAT_ROUNDS
    previous: Optional[List[dict]] = None
    for _round in range(rounds):
        # A plain chat's last round must answer. It offers the same tools, so its request extends the previous
        # one (the prompt-cache checkpoint needs that); the instruction came with the previous round's results.
        final = document is None and _round == rounds - 1
        # The round before it: its tool results carry that instruction.
        closing = document is None and _round == rounds - 2
        # The language pin is part of the history, after each user message: see `services.conversation`.
        messages = services.conversation(chat, document, language)
        # #138: how much of the previous round's request this one repeats; equal to `previous_messages` when it
        # extends it, which is what the backend's prompt-cache checkpoint needs.
        prefix = {
            "evilflowers.assistant.round": _round + 1,
            "evilflowers.assistant.prompt.messages": len(messages),
            "evilflowers.assistant.prompt.previous_messages": len(previous or []),
            "evilflowers.assistant.prompt.shared_messages": services.shared_prefix(previous, messages),
            "evilflowers.assistant.prompt.history_sha": history_hashes(messages),
        }
        previous = list(messages)
        services.mark_active()

        # On CPU inference a round can spend tens of seconds evaluating the prompt
        # before its first token (#104): tell the client something is happening.
        yield sse("status", {"state": "thinking", "round": _round + 1})
        resolver = citations.Resolver(ledger, request, state.cited, allowed=ledger.citable)
        # #162: until something was looked up, a content question's round is held back rather than streamed: if
        # it answers without looking, that answer is discarded, and the portal must not have shown it.
        guarded = content_question and not (state.retrieved or state.called or state.forced or state.attached or final)
        held: Optional[List[str]] = [] if guarded else None
        cited_before = set(state.cited)
        content, tool_calls, done = yield from _stream(client, messages, specifications, resolver, prefix, held)

        if not tool_calls and guarded and _from_memory(ledger, content, resolver):
            # It answered a question about content from memory (invented books, labels it was never given). The
            # turn looks the question up itself, stored as a call the model made, and asks again (#162).
            state.forced = True
            gathered = yield from _prefetch(
                request, chat, context, state, text, entry, fingerprint, spent=(done.total_tokens, done.model_seconds)
            )
            if gathered:
                # Nothing of that answer was sent, so none of its citations were either.
                state.cited.clear()
                state.cited.update(cited_before)
                continue
            # Nothing could be gathered: the answer it wrote is all there is.
        _keep_dropped(state, resolver)
        for frame in held or []:
            yield frame
        state.called = state.called or bool(tool_calls)

        if not tool_calls:
            if final and not content.strip() and not state.displayed:
                content = _notice(language, "rephrase")
                yield sse("message", {"text": content})
            yield from close(content, done.total_tokens, done.model_seconds)
            return

        # Showing cards or a place in the portal is the answer, not a lookup: when that is all the model asked
        # for, its text and the cards close the turn. Asking it again only made it call more tools until the
        # rounds ran out. Not when every id was refused (made-up ids, #163) or there is nothing to read with it:
        # then the model hears why and goes on.
        only_display = _closes(request, chat, tool_calls, content, staff)

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
            tokens_used=done.total_tokens,
            model_seconds=done.model_seconds,
            prompt_fingerprint=fingerprint,
        )

        if final and not only_display:
            # Out of rounds: every call is answered with the refusal, stored, so the next turn replays exactly
            # this request plus what follows (IP-030 §5). The model then answers from what it found (#164).
            for _call in tool_calls:
                ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=REFUSED)
            yield from _synthesise(request, chat, client, specifications, ledger, state, document, language, close)
            return

        for call in tool_calls:
            if not tools.is_directive((call.get("function") or {}).get("name") or ""):
                state.retrieved = True
            run = _run_tool(
                request,
                chat,
                call,
                document,
                state.pages_read,
                state.annotations,
                language,
                state.seen_calls,
                ledger,
                reading,
                context=context,
                note=ANSWER_NOW if closing else None,
                staff=staff,
            )
            for entry_id in (yield from run):
                if entry_id not in state.displayed:
                    state.displayed.append(entry_id)
            for event, data in context.frames:
                if event == "study":
                    state.study = data
                if event == "citation" and data.get("label") not in state.cited:
                    # Marks on a figure not cited yet (#179): its citation carries them when the answer cites it.
                    continue
                yield sse(event, data)
            context.frames.clear()

        if only_display:
            yield from close(content)
            return
        if state.study is not None:
            # A walkthrough, quiz or glossary card is the answer (like cards): the model does not restate it.
            yield from close(keep_empty=True)
            return

    # Out of rounds (a chat bound to a copy: the runaway guard).
    if state.displayed or state.annotations or state.study is not None:
        # Publications were already shown — that is an answer.
        yield from close(keep_empty=True)
        return

    # Nothing to show: say so rather than leaving the reader with silence.
    notice = _notice(language, "rephrase")
    yield sse("message", {"text": notice})
    yield from close(notice)


def _keep_dropped(state: _Turn, resolver: "citations.Resolver") -> None:
    for label in resolver.dropped:
        if label not in state.dropped:
            state.dropped.append(label)


#: Fixed notices a turn closes with when the model gave no answer, in the UI languages. The catalog has no Slovak
#: translation files, and a Slovak reader got these in English.
NOTICES = {
    "rephrase": {
        "sk": "Toto vyhľadávanie sa mi nepodarilo dokončiť. Skúsite to sformulovať inak?",
        "en": "I could not complete that search. Could you rephrase it?",
    },
    "not_found": {
        "sk": "V knižnici som to nenašla. Skúste sa opýtať inými slovami.",
        "en": "I could not find that in the library. Could you ask with other words?",
    },
}


def _notice(language: Optional[str], key: str) -> str:
    """A fixed notice in the reader's UI language (English for any other)."""
    code = (language or "").split("-")[0].lower()
    texts = NOTICES[key]
    return texts.get(code, texts["en"])


#: Words of a citing sentence and its passage that count as shared: long enough not to be function words.
_CONTENT_WORD = 5


def _from_memory(ledger: "evidence.Ledger", content: str, resolver: "citations.Resolver") -> bool:
    """Whether a round that called no tool answered a content question from the model's own knowledge (#162).

    Yes when it cited a label it was never given, or when the chat has no evidence at all (invented books on the
    first question). A follow-up in a chat with evidence ("thanks", "shorter please") is not looked up again when it
    cites nothing, or cites earlier labels whose passage shares a word with the sentence citing it; an earlier label
    with nothing in common (the copyright page cited for the Decorator pattern) is.
    """
    if resolver.dropped or not ledger.sources:
        return True
    for label, sentence in evidence.labels_with_sentences(content):
        passage = ledger.texts.get(label)
        if passage is None:
            continue
        said = {word for word in citations._words(sentence) if len(word) >= _CONTENT_WORD}
        source = {word for word in citations._words(passage) if len(word) >= _CONTENT_WORD}
        if not said & source:
            return True
    return False


def _closes(request, chat: Chat, tool_calls: List[dict], content: str, staff: bool) -> bool:
    """Whether a round's calls close the turn: only portal directives, and they show something (#163, #167).

    Cards close it when at least one id is real; a place in the portal only with text to read beside it, since
    the `navigate` frame is not stored and an empty answer would vanish when the chat is read back.
    """
    shows = False
    for call in tool_calls:
        function = call.get("function") or {}
        name = function.get("name") or ""
        if not tools.is_directive(name):
            return False
        arguments = _arguments(function.get("arguments"))
        if tools.is_display_books(name):
            accepted, _rejected = _displayable(request, chat, arguments.get("entry_ids"))
            shows = shows or bool(accepted)
        else:
            frame, _error, _rejected = portal.navigation(arguments, staff)
            shows = shows or (frame is not None and bool(content.strip()))
    return shows


#: An entry id inside a stored tool result.
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _displayable(request, chat: Chat, requested) -> tuple:
    """`(accepted, rejected)` publication ids of a `displayBooks` call (#163).

    A card is shown only for a publication the model was actually given: an id in one of this chat's tool results,
    the chat's publication or one the reader attached, and one this reader may see. A made-up id (an ISBN, a
    guessed UUID) is refused, never stored and never sent to the portal.
    """
    from apps.extraction.discovery import _visible

    if isinstance(requested, str):
        requested = [requested]
    requested = list(dict.fromkeys(str(item).strip() for item in requested or [] if str(item).strip()))
    if not requested:
        return [], []
    known = set()
    if chat.entry_id:
        known.add(str(chat.entry_id))
    for row in chat.messages.filter(role__in=(ChatMessage.Role.TOOL, ChatMessage.Role.USER)).values_list(
        "role", "text", "entry_id"
    ):
        role, text, entry_id = row
        if entry_id:
            known.add(str(entry_id))
        if role == ChatMessage.Role.TOOL and text:
            known.update(match.lower() for match in _UUID_TEXT.findall(text))
    for displayed in chat.messages.filter(displayed_entries__isnull=False).values_list("displayed_entries", flat=True):
        known.update(str(item) for item in displayed or [])

    candidates = [tools._uuid(item) for item in requested if tools._uuid(item) and item.lower() in known]
    visible = {str(pk) for pk in _visible(request, candidates)} if candidates else set()
    accepted = list(dict.fromkeys(item for item in candidates if item in visible))
    rejected = [item for item in requested if tools._uuid(item) not in accepted]
    return accepted, rejected


def _synthesise(request, chat: Chat, client, specifications, ledger, state: _Turn, document, language, close):
    """The answer after the last round's calls were refused (#164).

    Without it the turn closed on the text written beside the refused call ("Let me check …"), with no model call
    behind the answer. One more request, with the same tools so it extends the previous one; if it calls tools
    again, those calls are not stored (they would replay without results) and the reader gets a short notice.
    """
    messages = services.conversation(chat, document, language)
    yield sse("status", {"state": "thinking", "round": MAX_CHAT_ROUNDS + 1})
    resolver = citations.Resolver(ledger, request, state.cited, allowed=ledger.citable)
    content, tool_calls, done = yield from _stream(
        client, messages, specifications, resolver, {"evilflowers.assistant.round": MAX_CHAT_ROUNDS + 1}
    )
    _keep_dropped(state, resolver)
    if tool_calls or not content.strip():
        content = _notice(language, "not_found")
        yield sse("message", {"text": content})
    yield from close(content, done.total_tokens, done.model_seconds)


def _prefetch(request, chat: Chat, context: ToolContext, state: _Turn, text: str, entry, fingerprint: str, spent=None):
    """Gather the evidence pack and store it as a `gather_evidence` call (IP-030 §1); True when it was.

    Before round 1 when the deployment prefetches; after a round that answered a content question without
    looking anything up otherwise (#162). `spent` is that discarded round's `(tokens, seconds)`, booked on the
    stored call so the quota still counts it.
    """
    from apps.assistant import pack

    if not pack.needs_pack(text):
        return False
    try:
        payload = pack.gather(context, text, entry)
    except Exception:
        # A failed pack costs the speed-up, never the answer: the model still has its tools.
        logger.exception("Evidence pack failed (chat %s)", chat.pk)
        return False
    state.retrieved = True
    tokens, seconds = spent or (0, 0.0)
    call = {"function": {"name": tools.GATHER_EVIDENCE, "arguments": {"query": payload["query"]}}}
    # A model that asks for the same pack gets the repeated-call hint.
    _repeated(state.seen_calls, tools.GATHER_EVIDENCE, {"query": payload["query"]})
    yield sse(
        "status", {"state": "tool", "tool": tools.GATHER_EVIDENCE, **activity.describe(tools.GATHER_EVIDENCE, {})}
    )
    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.ASSISTANT,
        text="",
        tool_calls=[call],
        tokens_used=tokens,
        model_seconds=seconds,
        prompt_fingerprint=fingerprint,
    )
    model_text = json.dumps(pack.slim(payload), ensure_ascii=False, default=str)
    ChatMessage.objects.create(
        chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=model_text, tool_result=payload
    )
    yield sse("evidence", pack.sources(payload, request))
    yield sse("activity", activity.summarize(tools.GATHER_EVIDENCE, {"query": payload["query"]}, payload, model_text))
    return True


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
    request,
    chat: Chat,
    ledger: "evidence.Ledger",
    text: str,
    state: _Turn,
    tokens: int = 0,
    seconds: float = 0.0,
    keep_empty: bool = False,
    fingerprint: str = "",
    language: Optional[str] = None,
) -> Iterator[str]:
    """Store the answer that closes the turn, with what it showed, read, cited and taught.

    The evidence labels in `text` are resolved against the chat's ledger (IP-024) and completed for the viewer
    (IP-030 §2). A label the answer may not cite (`Ledger.citable`: unknown, or from an earlier turn once this one
    has evidence) was already taken out while streaming, so the stored answer is still the one the reader saw;
    it is reported under `unknown` (#162). A study card's items cite labels of their own; they are resolved too
    (#145).
    """
    text, dropped = evidence.keep_citations(text, ledger.citable)
    found, unknown = ledger.cite(text, request)
    for label in [*state.dropped, *dropped]:
        if label not in unknown:
            unknown.append(label)
    if state.study is not None:
        _study_citations(ledger, state.study, request, found, unknown)
    if unknown:
        logger.warning("Assistant answer cited labels it was not given %s (chat %s)", unknown, chat.pk)
    row = ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.ASSISTANT,
        text=text,
        # An empty list (rather than None) keeps a textless answer visible (`visible_q`).
        displayed_entries=state.displayed if keep_empty else (state.displayed or None),
        pages_read=state.pages_read or None,
        annotations=state.annotations or None,
        citations=found or None,
        study=state.study,
        tokens_used=tokens,
        model_seconds=seconds,
        prompt_fingerprint=fingerprint,
    )
    state.message_id = row.pk
    yield from _pages_frame(state.pages_read)
    if found or unknown:
        yield sse("citations", {"citations": found, "unknown": unknown})
    chips = suggestion_chips.build(found, state.study, language)
    if chips:
        yield sse("suggestions", {"suggestions": chips})


#: The text of a study item a label belongs to, by mode: what `citations.complete` matches the sentence against.
_STUDY_ITEM_TEXT = {
    "walkthrough": ("points", ("text",)),
    "quiz": ("questions", ("question", "explanation")),
    "key_concepts": ("concepts", ("term", "definition")),
}


def _study_citations(ledger: "evidence.Ledger", study: dict, request, found: List[dict], unknown: List[str]) -> None:
    """Add the labels on a study card's items to `found` / `unknown`, in place, each once, in item order (#145).

    A study turn closes with empty text, so `ledger.cite` sees none of them; without this, a card's chips have no
    page, paragraph or quote to open.
    """
    items_key, text_keys = _STUDY_ITEM_TEXT.get(str(study.get("mode") or ""), (None, ()))
    data = study.get("data") or {}
    items = data.get(items_key) if items_key else None
    if not isinstance(items, list):
        return
    seen = {citation["label"] for citation in found} | set(unknown)
    for item in items:
        if not isinstance(item, dict):
            continue
        sentence = " ".join(str(item.get(key) or "") for key in text_keys).strip()
        for label in item.get("labels") or []:
            label = evidence.normalise(str(label))
            if not label or label in seen:
                continue
            seen.add(label)
            source = ledger.sources.get(label)
            if source is None:
                unknown.append(label)
            else:
                found.append(citations.complete(label, source, sentence, request))


def _pages_frame(pages_read: List[int]) -> Iterator[str]:
    """Tells the client which pages the answer was drawn from."""
    if pages_read:
        yield sse("pages", {"pages": pages_read})


def _stream(client, messages: List[dict], specifications, resolver=None, span_attributes=None, held=None):
    """Consume one completion, forwarding text chunks (and the citations they complete) as they arrive.

    Returns `(content, tool_calls, done delta)`. The `chunk` frames, concatenated, are exactly `content`: with a
    `resolver`, the text as it let it through (labels the answer may not cite taken out). With a `held` list the
    frames are appended to it instead of yielded, for the caller to send or drop once the round is over.
    """
    content = ""
    tool_calls: List[dict] = []
    done = Delta(done=True)

    def frames(text: str, found: List[dict]) -> Iterator[str]:
        if text:
            yield sse("chunk", {"text": text})
        for citation in found:
            yield sse("citation", citation)

    def out(produced: Iterator[str]) -> Iterator[str]:
        for frame in produced:
            if held is None:
                yield frame
            else:
                held.append(frame)

    for delta in client.chat(
        messages,
        tools=specifications,
        span_attributes=span_attributes,
        max_tokens=settings.EVILFLOWERS_ASSISTANT_NUM_PREDICT or None,
    ):
        if delta.content:
            content += delta.content
            if resolver is None:
                yield from out(iter([sse("chunk", {"text": delta.content})]))
            else:
                yield from out(frames(*resolver.feed(delta.content)))
        if delta.tool_calls:
            tool_calls.extend(delta.tool_calls)
        if delta.done:
            done = delta
    if resolver is not None:
        yield from out(frames(*resolver.finish()))
        content = resolver.text

    if content:
        yield from out(iter([sse("message", {"text": content})]))

    return content, tool_calls, done


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


def _with_note(text: str, note: Optional[str]) -> str:
    """A tool result with the closing instruction added (IP-030 §5); stored so the replay matches."""
    if not note:
        return text
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        value["instruction"] = note
        return json.dumps(value, ensure_ascii=False, default=str)
    return json.dumps({"result": value if value is not None else text, "instruction": note}, ensure_ascii=False)


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
    context: Optional[ToolContext] = None,
    note: Optional[str] = None,
    staff: bool = False,
) -> Generator[str, None, List[str]]:
    """Run one tool call, yielding SSE frames; returns publication ids to display."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    arguments = _arguments(function.get("arguments"))

    if not tools.is_display_books(name):
        if _repeated(seen_calls, name, arguments):
            ChatMessage.objects.create(
                chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=_with_note(REPEATED_CALL, note)
            )
            return []
        yield sse("status", {"state": "tool", "tool": name, **activity.describe(name, arguments)})

    if tools.is_display_books(name):
        entry_ids, rejected = _displayable(request, chat, arguments.get("entry_ids"))
        # The tool row answers the call for the model's replay; the ids
        # themselves are persisted on the turn's final answer (see `_rounds`).
        text = ""
        if rejected:
            # Made-up ids never reach the portal or the stored answer (#163); the model hears which.
            text = json.dumps(
                {
                    "error": f"Not shown, not ids from a tool result in this chat: {', '.join(rejected)}. Use the "
                    "`id` of publications that search_entries or another tool returned.",
                    "shown": entry_ids,
                },
                ensure_ascii=False,
            )
            logger.warning("displayBooks refused ids %s (chat %s)", rejected, chat.pk)
        ChatMessage.objects.create(
            chat=chat,
            user=chat.user,
            role=ChatMessage.Role.TOOL,
            text=_with_note(text, note) if note else text,
        )
        if entry_ids:
            yield sse("entries", {"entry_ids": entry_ids})
        return entry_ids

    if tools.is_show_in_portal(name):
        frame, error, _rejected = portal.navigation(arguments, staff)
        result = {"shown": frame is not None}
        if error:
            result["error"] = error
        text = json.dumps(result, ensure_ascii=False)
        ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=_with_note(text, note))
        if frame is not None:
            yield sse("navigate", frame)
        yield sse("activity", activity.summarize(name, arguments, text="" if frame is not None else text))
        return []

    if (
        document is not None
        and name == CREATE_HIGHLIGHTS
        and CREATE_HIGHLIGHTS_SPECIFICATION in _offered(chat, document)
    ):
        text = yield from _create_highlights(chat, arguments, annotations if annotations is not None else [], language)
        ChatMessage.objects.create(chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=_with_note(text, note))
        yield sse("activity", activity.summarize(name, arguments, text=text))
        return []

    if document is not None and name in document_tools.TOOLS and not _another_publication(document, arguments):
        # The reader's own copy, read in-process exactly as explain reads it, with evidence labels (IP-030 Q1).
        pages = pages_read if pages_read is not None else []
        # Pages read from other publications this turn count against the same budget (#127).
        budget = None
        if reading is not None:
            budget = max(settings.EVILFLOWERS_ASSISTANT_MAX_PAGES_READ - sum(map(len, reading.books.values())), 0)
        payload, text = document_tools.run(
            document, call, pages, budget, ledger if ledger is not None else evidence.Ledger()
        )
        ChatMessage.objects.create(
            chat=chat, user=chat.user, role=ChatMessage.Role.TOOL, text=_with_note(text, note), tool_result=payload
        )
        yield sse("activity", activity.summarize(name, arguments, payload, text))
        return []

    spent = context.model_seconds if context is not None else 0.0
    try:
        # The model sees a slim result; the full payload is kept on the row (IP-022).
        payload, text = tools.run(request, name, arguments, ledger=ledger, reading=reading, context=context)
    except LLMUnavailable:
        raise
    except Exception:
        logger.exception("Assistant tool '%s' raised", name)
        text = json.dumps({"error": "The tool failed."})
        payload = None

    ChatMessage.objects.create(
        chat=chat,
        user=chat.user,
        role=ChatMessage.Role.TOOL,
        text=_with_note(text, note),
        tool_result=payload,
        # Model time of the tool's own requests (study, summary, vision) counts against the quota too (Q2).
        model_seconds=round((context.model_seconds - spent) if context is not None else 0.0, 3),
    )
    yield sse("activity", activity.summarize(name, arguments, payload, text))
    return []

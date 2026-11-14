"""Model clients for the assistant: Ollama, and llama.cpp's llama-server (IP-030 §5).

IP-015 D9: native tool calling, rather than the reference implementation's
`/api/generate` plus a `<tool_calls>` block hand-parsed out of the token stream.
Native calls arrive structured, which is what the message model stores, and a
message array maps one-to-one onto `assistant_messages` rows.

Both clients take the same messages (Ollama's shape, which is what the rows
store) and yield the same `Delta`s; `client()` picks one by
`EVILFLOWERS_ASSISTANT_BACKEND`. llama-server adds what Ollama lacks for this
hybrid model: a chat's slot is saved to disk after its turn and restored before
the next one (`apps.assistant.slots`), so a follow-up after a pause, after
another chat or after a pod restart evaluates only the new tokens.

Streaming is a plain blocking read. The project serves requests with gevent
workers (`conf/gunicorn.conf.py`), so a socket read yields the greenlet instead
of pinning an OS thread, and a long generation does not cost a worker.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Iterator, List, Optional
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.utils.translation import gettext as _
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from evil_flowers_catalog import telemetry

logger = logging.getLogger(__name__)

# GenAI semantic conventions (IP-019). Only names, counts and the model go into
# attributes — never prompt, completion or document text.
PROVIDER_NAME = "ollama"
OPERATION_NAME = "chat"

OLLAMA = "ollama"
LLAMA_SERVER = "llama-server"

_tracer = telemetry.get_tracer("apps.assistant")
_meter = telemetry.get_meter("apps.assistant")
_operation_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="GenAI operation duration.",
    explicit_bucket_boundaries_advisory=[
        0.01,
        0.02,
        0.04,
        0.08,
        0.16,
        0.32,
        0.64,
        1.28,
        2.56,
        5.12,
        10.24,
        20.48,
        40.96,
        81.92,
    ],
)
_token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Number of input and output tokens used.",
    explicit_bucket_boundaries_advisory=[
        1,
        4,
        16,
        64,
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
        16777216,
        67108864,
    ],
)


class LLMUnavailable(Exception):
    """The inference backend is unreachable, misconfigured or failed."""


@dataclass
class Delta:
    """One event from a streamed completion.

    Exactly one of `content` or `tool_calls` is meaningful on any given delta;
    `done` marks the final event, which also carries the token and time accounting.
    """

    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Model time of the request (IP-030 Q2): prompt evaluation and generation, as the backend measured them.
    prompt_seconds: float = 0.0
    generation_seconds: float = 0.0
    #: Prompt tokens reused from the cache (llama-server's `timings.cache_n`); None when the backend does not say.
    cached_tokens: Optional[int] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def model_seconds(self) -> float:
        return round(self.prompt_seconds + self.generation_seconds, 3)


def _optional_bool(value) -> Optional[bool]:
    """'0'/'1'/'' (or a bool) as False/True/None — None means "do not send"."""
    if isinstance(value, bool) or value is None:
        return value
    value = str(value).strip().lower()
    if value in ("", "none"):
        return None
    return value not in ("0", "false", "no", "off")


def _optional_float(value) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def backend() -> str:
    return (getattr(settings, "EVILFLOWERS_ASSISTANT_BACKEND", OLLAMA) or OLLAMA).strip().lower()


def client():
    """The configured model client (IP-030 §5)."""
    if backend() == LLAMA_SERVER:
        return LlamaServerClient()
    return OllamaClient()


class OllamaClient:
    provider = PROVIDER_NAME

    def __init__(self):
        self.endpoint = settings.EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT
        self.model = settings.EVILFLOWERS_ASSISTANT_OLLAMA_MODEL
        self.api_key = settings.EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY
        self.timeout = settings.EVILFLOWERS_ASSISTANT_OLLAMA_TIMEOUT
        self.connect_timeout = getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_CONNECT_TIMEOUT", 10)
        self.host = getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_HOST", "")
        self.think = _optional_bool(getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_THINK", "0"))
        self.num_ctx = int(getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_NUM_CTX", 0) or 0)
        self.keep_alive = str(getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_KEEP_ALIVE", "") or "").strip()
        self.temperature = _optional_float(getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_TEMPERATURE", ""))

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.model)

    # A chat turn brackets its rounds with these (`turn.run`); Ollama keeps no state the catalog can manage.
    def begin_turn(self, key: str) -> None:
        pass

    def end_turn(self) -> None:
        pass

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        # Cloud Ollama authenticates with a bearer token; a local instance needs
        # none. Sending an empty header would be rejected by some proxies, so
        # the key is omitted entirely when unset.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # The Compose catalog reaches the cluster's Ollama through the internal
        # Gateway, which routes by Host (as EVILFLOWERS_EMBEDDING_HOST does for TEI).
        if self.host:
            headers["Host"] = self.host
        return headers

    def payload(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        response_format=None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """The `/api/chat` request body.

        Everything here is per deployment, never per request, so two requests
        with the same messages render the same prompt and hit the same cache.
        `response_format` is Ollama's `format`: "json" or a JSON Schema the
        answer is constrained to (only for callers that offer no tools).
        `max_tokens` is `num_predict`, a runaway cap that is not part of the prompt.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if response_format is not None:
            payload["format"] = response_format
        if self.think is not None:
            payload["think"] = self.think
        if self.keep_alive:
            # Ollama takes a duration string ("10m") or a number of seconds (-1 = forever).
            try:
                payload["keep_alive"] = int(self.keep_alive)
            except ValueError:
                payload["keep_alive"] = self.keep_alive

        options = {}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        if self.temperature is not None:
            options["temperature"] = self.temperature
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        if options:
            payload["options"] = options
        return payload

    def _url(self) -> str:
        return self.endpoint

    def _request_payload(self, messages, tools, response_format, max_tokens) -> dict:
        return self.payload(messages, tools, response_format, max_tokens)

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        response_format=None,
        span_attributes: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[Delta]:
        """Stream a completion, yielding a `Delta` per event.

        `messages` is sent verbatim and in full. Callers must not rewrite
        earlier turns between requests: an identical prefix is what lets the
        backend reuse its KV cache (IP-015 D3).
        """
        if not self.configured:
            raise LLMUnavailable(_("The assistant is not configured: set the model endpoint and model."))

        payload = self._request_payload(messages, tools, response_format, max_tokens)

        # One CLIENT span for the model call (IP-019). It is not made current
        # across the `yield`s below — a generator resumes in its consumer's
        # context — only around the HTTP request, whose own span is suppressed so
        # the call shows up once in Tempo's service graph.
        attributes = self._telemetry_attributes()
        span = _tracer.start_span(
            f"{OPERATION_NAME} {self.model}",
            kind=SpanKind.CLIENT,
            # Extra attributes go on the span only, not on the metrics `attributes` also labels.
            attributes={**attributes, **(span_attributes or {})},
        )
        stopwatch = telemetry.Stopwatch()
        error_type = None

        try:
            try:
                with trace.use_span(span, end_on_exit=False), telemetry.suppress_http_spans():
                    response = requests.post(
                        self._url(),
                        json=payload,
                        headers=self._headers(),
                        stream=True,
                        # (connect, read): the read timeout applies between bytes, so
                        # it is what a slow prompt evaluation before the first token hits.
                        timeout=(self.connect_timeout, self.timeout),
                    )
                    response.raise_for_status()
            except requests.RequestException as error:
                error_type = self._request_error_type(error)
                logger.exception("Model request failed (%s)", self.provider)
                raise LLMUnavailable(str(error)) from error

            try:
                for delta in self._deltas(response, span, attributes):
                    yield delta
            except LLMUnavailable:
                error_type = error_type or f"{self.provider}_error"
                raise
            except requests.RequestException as error:
                error_type = self._request_error_type(error)
                raise
            finally:
                # Closing the response closes the connection: the backend stops generating for a reader
                # who left (a client disconnect closes this generator, IP-030 §5).
                response.close()
        except GeneratorExit:
            # The consumer stopped reading (client disconnected); not a failure.
            span.set_attribute("evilflowers.assistant.cancelled", True)
            raise
        except Exception as error:
            error_type = error_type or type(error).__qualname__
            raise
        finally:
            if error_type:
                attributes["error.type"] = error_type
                span.set_attribute("error.type", error_type)
                span.set_status(StatusCode.ERROR)
            span.end()
            _operation_duration.record(stopwatch.elapsed, attributes)

    def _deltas(self, response, span, attributes) -> Iterator[Delta]:
        thinking_chunks = 0
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A malformed line is not worth killing a live conversation
                # over; skip it and keep reading the stream.
                logger.warning("Skipping malformed line from Ollama: %r", line[:200])
                continue

            if event.get("error"):
                raise LLMUnavailable(str(event["error"]))

            message = event.get("message") or {}
            if message.get("thinking"):
                # Reasoning is never shown or stored; it only costs time. Counted,
                # so a model that thinks despite `think: false` shows in traces.
                thinking_chunks += 1
            delta = Delta(
                content=message.get("content") or "",
                tool_calls=message.get("tool_calls") or [],
                done=bool(event.get("done")),
                prompt_tokens=event.get("prompt_eval_count") or 0,
                completion_tokens=event.get("eval_count") or 0,
                prompt_seconds=(event.get("prompt_eval_duration") or 0) / 1e9,
                generation_seconds=(event.get("eval_duration") or 0) / 1e9,
            )
            if delta.done:
                self._record_usage(span, attributes, event.get("model"), delta)
                if thinking_chunks:
                    span.set_attribute("evilflowers.assistant.thinking_chunks", thinking_chunks)
                self._record_timings(span, event)

            yield delta

    def _telemetry_attributes(self) -> dict:
        attributes = {
            "gen_ai.operation.name": OPERATION_NAME,
            "gen_ai.provider.name": self.provider,
            "gen_ai.request.model": self.model,
        }
        address = urlsplit(self._url() or "")
        if address.hostname:
            attributes["server.address"] = address.hostname
            attributes["server.port"] = address.port or (443 if address.scheme == "https" else 80)
        return attributes

    @staticmethod
    def _request_error_type(error: requests.RequestException) -> str:
        response = getattr(error, "response", None)
        if response is not None and response.status_code:
            return str(response.status_code)
        return type(error).__qualname__

    @staticmethod
    def _record_timings(span, event: dict) -> None:
        """Ollama's own durations (nanoseconds) on the span: on CPU, prompt evaluation
        is the number to watch (#104), and it is not separable from the wall clock."""
        for key in ("load_duration", "prompt_eval_duration", "eval_duration"):
            value = event.get(key)
            if value:
                span.set_attribute(f"evilflowers.ollama.{key}_s", round(value / 1e9, 3))

    @staticmethod
    def _record_usage(span, attributes: dict, model: Optional[str], delta: Delta) -> None:
        if model:
            span.set_attribute("gen_ai.response.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", delta.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", delta.completion_tokens)
        span.set_attribute("evilflowers.assistant.model_seconds", delta.model_seconds)
        if delta.cached_tokens is not None:
            span.set_attribute("evilflowers.assistant.cached_tokens", delta.cached_tokens)
        _token_usage.record(delta.prompt_tokens, {**attributes, "gen_ai.token.type": "input"})
        _token_usage.record(delta.completion_tokens, {**attributes, "gen_ai.token.type": "output"})


def to_openai(messages: List[dict]) -> List[dict]:
    """Stored (Ollama-shaped) messages in the OpenAI chat format llama-server takes.

    Deterministic, so a replayed conversation renders the same prompt every time: a tool call's id is its
    position (`call_<message>_<call>`), its arguments are a JSON string in the order they were stored, and each
    tool result answers the oldest unanswered call before it. Images (`images`: base64) become image parts.
    """
    converted: List[dict] = []
    pending: List[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for number, call in enumerate(message["tool_calls"]):
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                call_id = f"call_{index}_{number}"
                pending.append(call_id)
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": function.get("name"), "arguments": arguments},
                    }
                )
            converted.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": pending.pop(0) if pending else f"call_{index}_0",
                    "content": message.get("content") or "",
                }
            )
        elif message.get("images"):
            parts = [{"type": "text", "text": message.get("content") or ""}]
            for image in message["images"]:
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}})
            converted.append({"role": role, "content": parts})
        else:
            converted.append({"role": role, "content": message.get("content") or ""})
    return converted


class LlamaServerClient(OllamaClient):
    """llama.cpp's llama-server over its OpenAI-compatible API (IP-030 §5, Q11).

    The same GGUF as Ollama's `elvira-assistant`, started with `--jinja` (tool calls through the model's chat
    template), context checkpoints and `--slot-save-path`. Every request runs on a slot the catalog leases
    (`apps.assistant.slots`): a chat turn leases one for all its rounds and restores the chat's saved state
    first; any other caller (explain, highlight, summaries) leases one for its request, so it never lands on a
    slot a chat turn holds between two rounds.
    """

    provider = LLAMA_SERVER

    def __init__(self):
        super().__init__()
        self.base = (getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_ENDPOINT", "") or "").rstrip("/")
        self.endpoint = f"{self.base}/v1/chat/completions" if self.base else ""
        self.model = getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_MODEL", "") or self.model
        self.host = getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_HOST", "") or ""
        self.api_key = None
        self.top_p = _optional_float(getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_TOP_P", ""))
        top_k = _optional_float(getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_TOP_K", ""))
        self.top_k = int(top_k) if top_k is not None else None
        #: The slot of the current chat turn (`begin_turn`), else None.
        self.lease = None
        self._turn_key: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.base and self.model)

    # ----------------------------------------------------------- slots

    def begin_turn(self, key: str) -> None:
        """Lease a slot for this chat's turn and restore the chat's saved state into it."""
        from apps.assistant import slots

        if not self.configured:
            return
        self.lease = slots.acquire()
        self._turn_key = key
        slots.restore(self, self.lease.slot, key)

    def end_turn(self) -> None:
        """Save the chat's state from its slot and give the slot back. Never raises."""
        from apps.assistant import slots

        lease, key = self.lease, self._turn_key
        self.lease, self._turn_key = None, None
        if lease is None:
            return
        try:
            if key and lease.owned():
                slots.save(self, lease.slot, key)
            elif key:
                logger.warning("Slot %s lease expired during the turn of chat %s; not saved", lease.slot, key)
        finally:
            lease.release()

    def slot_action(self, slot: int, action: str, filename: str) -> Optional[dict]:
        """`POST /slots/{id}?action=save|restore`; None when the server refused (no such file, say)."""
        try:
            response = requests.post(
                f"{self.base}/slots/{slot}",
                params={"action": action},
                json={"filename": filename},
                headers=self._headers(),
                timeout=(self.connect_timeout, self.timeout),
            )
        except requests.RequestException as error:
            logger.warning("llama-server slot %s %s failed: %s", action, slot, error)
            return None
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return {}

    # ----------------------------------------------------------- requests

    def payload(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        response_format=None,
        max_tokens: Optional[int] = None,
        slot: Optional[int] = None,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": to_openai(messages),
            "stream": True,
            # Reuse the slot's KV cache (and its checkpoints) for the common prefix.
            "cache_prompt": True,
        }
        if tools:
            payload["tools"] = tools
        if response_format is not None:
            if isinstance(response_format, dict):
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": response_format},
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        if self.think is not None:
            # Qwen's chat template switch; `think: false` in Ollama.
            payload["chat_template_kwargs"] = {"enable_thinking": bool(self.think)}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.top_k is not None:
            payload["top_k"] = self.top_k
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if slot is not None:
            payload["id_slot"] = slot
        return payload

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        response_format=None,
        span_attributes: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[Delta]:
        from apps.assistant import slots

        if self.lease is not None:
            self.lease.refresh()
            yield from self._chat_on(self.lease.slot, messages, tools, response_format, span_attributes, max_tokens)
            return
        if not self.configured:
            raise LLMUnavailable(_("The assistant is not configured: set the model endpoint and model."))
        lease = slots.acquire()
        try:
            yield from self._chat_on(lease.slot, messages, tools, response_format, span_attributes, max_tokens)
        finally:
            lease.release()

    def _chat_on(self, slot, messages, tools, response_format, span_attributes, max_tokens):
        self._slot = slot
        attributes = {**(span_attributes or {}), "evilflowers.assistant.slot": slot}
        yield from super().chat(messages, tools, response_format, attributes, max_tokens)

    def _request_payload(self, messages, tools, response_format, max_tokens):
        return self.payload(messages, tools, response_format, max_tokens, getattr(self, "_slot", None))

    def _deltas(self, response, span, attributes) -> Iterator[Delta]:
        calls: dict = {}
        usage: dict = {}
        timings: dict = {}
        model = None
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line from llama-server: %r", line[:200])
                continue
            if event.get("error"):
                raise LLMUnavailable(
                    str(event["error"].get("message") if isinstance(event["error"], dict) else event["error"])
                )
            model = event.get("model") or model
            usage = event.get("usage") or usage
            timings = event.get("timings") or timings
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(call.get("index", len(calls)), {"name": "", "arguments": ""})
                    function = call.get("function") or {}
                    slot["name"] += function.get("name") or ""
                    slot["arguments"] += function.get("arguments") or ""
                if delta.get("content"):
                    yield Delta(content=delta["content"])

        if calls:
            yield Delta(tool_calls=[_ollama_call(calls[index]) for index in sorted(calls)])
        done = Delta(
            done=True,
            # The whole prompt, reused tokens included, as Ollama counts it; `cached_tokens` says how many.
            prompt_tokens=(
                int(timings.get("prompt_n") or 0) + int(timings.get("cache_n") or 0)
                if timings
                else int(usage.get("prompt_tokens") or 0)
            ),
            completion_tokens=int(usage.get("completion_tokens") or timings.get("predicted_n") or 0),
            prompt_seconds=float(timings.get("prompt_ms") or 0) / 1000,
            generation_seconds=float(timings.get("predicted_ms") or 0) / 1000,
            cached_tokens=int(timings["cache_n"]) if timings.get("cache_n") is not None else None,
        )
        self._record_usage(span, attributes, model, done)
        for key in ("prompt_ms", "predicted_ms"):
            if timings.get(key):
                span.set_attribute(f"evilflowers.llama.{key[:-3]}_s", round(float(timings[key]) / 1000, 3))
        yield done


def _ollama_call(call: dict) -> dict:
    """A streamed OpenAI tool call in the shape rows store (Ollama's: arguments as an object)."""
    try:
        arguments = json.loads(call["arguments"]) if call["arguments"] else {}
    except json.JSONDecodeError:
        arguments = call["arguments"]
    return {"function": {"name": call["name"], "arguments": arguments}}

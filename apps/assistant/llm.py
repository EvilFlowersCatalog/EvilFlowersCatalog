"""Ollama client for the assistant.

IP-015 D9: `/api/chat` with native tool calling, rather than the reference
implementation's `/api/generate` plus a `<tool_calls>` block hand-parsed out of
the token stream. Native calls arrive structured, which is what the message
model stores, and a message array maps one-to-one onto `assistant_messages`
rows.

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
    `done` marks the final event, which also carries the token accounting.
    """

    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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


class OllamaClient:
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

    def payload(self, messages: List[dict], tools: Optional[List[dict]] = None, response_format=None) -> dict:
        """The `/api/chat` request body.

        Everything here is per deployment, never per request, so two requests
        with the same messages render the same prompt and hit the same cache.
        `response_format` is Ollama's `format`: "json" or a JSON Schema the
        answer is constrained to (only for callers that offer no tools).
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
        if options:
            payload["options"] = options
        return payload

    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None, response_format=None) -> Iterator[Delta]:
        """Stream a completion, yielding a `Delta` per event.

        `messages` is sent verbatim and in full. Callers must not rewrite
        earlier turns between requests: an identical prefix is what lets the
        backend reuse its KV cache (IP-015 D3).
        """
        if not self.configured:
            raise LLMUnavailable(_("The assistant is not configured: set the Ollama endpoint and model."))

        payload = self.payload(messages, tools, response_format)

        # One CLIENT span for the model call (IP-019). It is not made current
        # across the `yield`s below — a generator resumes in its consumer's
        # context — only around the HTTP request, whose own span is suppressed so
        # the call shows up once in Tempo's service graph.
        attributes = self._telemetry_attributes()
        span = _tracer.start_span(f"{OPERATION_NAME} {self.model}", kind=SpanKind.CLIENT, attributes=attributes)
        stopwatch = telemetry.Stopwatch()
        error_type = None
        thinking_chunks = 0

        try:
            try:
                with trace.use_span(span, end_on_exit=False), telemetry.suppress_http_spans():
                    response = requests.post(
                        self.endpoint,
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
                logger.exception("Ollama request failed")
                raise LLMUnavailable(str(error)) from error

            try:
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
                        error_type = "ollama_error"
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
                    )
                    if delta.done:
                        self._record_usage(span, attributes, event, delta)
                        if thinking_chunks:
                            span.set_attribute("evilflowers.assistant.thinking_chunks", thinking_chunks)
                        self._record_timings(span, event)

                    yield delta
            except requests.RequestException as error:
                error_type = self._request_error_type(error)
                raise
            finally:
                response.close()
        except GeneratorExit:
            # The consumer stopped reading (client disconnected); not a failure.
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

    def _telemetry_attributes(self) -> dict:
        attributes = {
            "gen_ai.operation.name": OPERATION_NAME,
            "gen_ai.provider.name": PROVIDER_NAME,
            "gen_ai.request.model": self.model,
        }
        address = urlsplit(self.endpoint)
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
    def _record_usage(span, attributes: dict, event: dict, delta: Delta) -> None:
        if event.get("model"):
            span.set_attribute("gen_ai.response.model", event["model"])
        span.set_attribute("gen_ai.usage.input_tokens", delta.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", delta.completion_tokens)
        _token_usage.record(delta.prompt_tokens, {**attributes, "gen_ai.token.type": "input"})
        _token_usage.record(delta.completion_tokens, {**attributes, "gen_ai.token.type": "output"})

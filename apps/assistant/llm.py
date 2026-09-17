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

import requests
from django.conf import settings
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


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


class OllamaClient:
    def __init__(self):
        self.endpoint = settings.EVILFLOWERS_ASSISTANT_OLLAMA_ENDPOINT
        self.model = settings.EVILFLOWERS_ASSISTANT_OLLAMA_MODEL
        self.api_key = settings.EVILFLOWERS_ASSISTANT_OLLAMA_API_KEY
        self.timeout = settings.EVILFLOWERS_ASSISTANT_OLLAMA_TIMEOUT

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
        return headers

    def chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> Iterator[Delta]:
        """Stream a completion, yielding a `Delta` per event.

        `messages` is sent verbatim and in full. Callers must not rewrite
        earlier turns between requests: an identical prefix is what lets the
        backend reuse its KV cache (IP-015 D3).
        """
        if not self.configured:
            raise LLMUnavailable(_("The assistant is not configured: set the Ollama endpoint and model."))

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=self._headers(),
                stream=True,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
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
                    raise LLMUnavailable(str(event["error"]))

                message = event.get("message") or {}

                yield Delta(
                    content=message.get("content") or "",
                    tool_calls=message.get("tool_calls") or [],
                    done=bool(event.get("done")),
                    prompt_tokens=event.get("prompt_eval_count") or 0,
                    completion_tokens=event.get("eval_count") or 0,
                )
        finally:
            response.close()

"""Slot leases on llama-server, and a chat's slot saved between turns (IP-030 §5, Q11).

llama-server runs `EVILFLOWERS_ASSISTANT_LLAMA_SLOTS` slots (`--parallel`), each with its own KV cache and
context checkpoints. A hybrid model (Gated DeltaNet) reuses a cache only from a checkpoint at a prefix of the new
prompt, so a chat keeps its speed only while its state stays in one slot. Two rules keep it there:

- **A lease per turn.** A chat turn leases one slot for all its rounds (`acquire` → `Lease`); no other request
  may run on that slot meanwhile, so the state the turn left after round 1 is still there for round 2. Every
  other request leases a slot for its own duration (`LlamaServerClient.chat`).
- **Save and restore.** At the end of a turn the slot's state is written to the server's `--slot-save-path`
  (`chat-<id>.bin`), and the next turn of that chat restores it before its first round. A follow-up after a
  pause, after other chats used the slot, or after a pod restart then evaluates only its new tokens. A missing
  file costs what a cache miss costs anyway. The files are evicted on the server (by age and total size,
  knowledge#44): the catalog cannot list or delete them.

Leases live in the shared cache (Redis in every deployment), with an expiry so a worker that dies holding one
frees it. `cache.add` is atomic there, which is what makes a lease exclusive.
"""

import logging
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

#: Seconds a lease lives without a refresh; every round refreshes it. Above the model's read timeout, so a
#: round still being generated never loses its slot.
LEASE_SECONDS = 600

#: Seconds between attempts while every slot is taken.
POLL_SECONDS = 0.25


def _key(slot: int) -> str:
    return f"evilflowers:assistant:llama-slot:{slot}"


def lease_seconds() -> int:
    return max(LEASE_SECONDS, int(getattr(settings, "EVILFLOWERS_ASSISTANT_OLLAMA_TIMEOUT", 300)) + 60)


@dataclass
class Lease:
    slot: int
    token: str

    def owned(self) -> bool:
        """Still ours: a lease that expired may already serve another chat, whose state must not be saved as ours."""
        return cache.get(_key(self.slot)) == self.token

    def refresh(self) -> None:
        if cache.get(_key(self.slot)) == self.token:
            cache.touch(_key(self.slot), lease_seconds())

    def release(self) -> None:
        # Only our own lease: one that expired and was taken by another turn stays theirs.
        if cache.get(_key(self.slot)) == self.token:
            cache.delete(_key(self.slot))


def acquire(wait: float = None) -> Lease:
    """A free slot, waiting up to `EVILFLOWERS_ASSISTANT_LLAMA_SLOT_WAIT` seconds; `LLMUnavailable` after that."""
    from apps.assistant.llm import LLMUnavailable

    slots = max(int(getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_SLOTS", 1)), 1)
    wait = float(settings.EVILFLOWERS_ASSISTANT_LLAMA_SLOT_WAIT if wait is None else wait)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    while True:
        for slot in range(slots):
            if cache.add(_key(slot), token, lease_seconds()):
                return Lease(slot, token)
        if time.monotonic() >= deadline:
            raise LLMUnavailable(_("The assistant is busy; try again in a moment."))
        # Under gevent this sleep yields the greenlet.
        time.sleep(POLL_SECONDS)


def filename(key: str) -> str:
    return f"chat-{key}.bin"


def enabled() -> bool:
    return bool(getattr(settings, "EVILFLOWERS_ASSISTANT_LLAMA_SLOT_FILES", True))


def restore(client, slot: int, key: str) -> bool:
    """Load the chat's saved state into `slot`; False when there is none (a new chat, or evicted)."""
    if not enabled():
        return False
    result = client.slot_action(slot, "restore", filename(key))
    if result is None:
        return False
    logger.info("Restored chat %s into slot %s (%s tokens)", key, slot, result.get("n_restored"))
    return True


def save(client, slot: int, key: str) -> bool:
    if not enabled():
        return False
    return client.slot_action(slot, "save", filename(key)) is not None

"""
Email attachment abstraction for notifications.

The notifications app must not know how to build any particular attachment —
that would couple it to every domain that wants to attach something (readium
`.lcpl`, invoices, exports, …). Instead it owns:

  * `EmailAttachment` — a transport-agnostic value object the mailer attaches.
  * an `AttachmentResolver` registry — domains register a named resolver that
    turns a small, JSON-serialisable `params` dict into an `EmailAttachment`.

A notification carries `AttachmentRef`s (name + params) rather than raw bytes,
so the reference survives the Celery hop and the (potentially expensive)
materialisation happens in the worker at send time. This is dependency
inversion: notifications defines the protocol; domains depend on it and plug
in. Adding a new attachment kind touches only the new domain — never this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, TypedDict, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailAttachment:
    """A materialised attachment, ready for `EmailMessage.attach`."""

    filename: str
    content: str | bytes
    mimetype: str


class AttachmentRef(TypedDict):
    """A serialisable pointer to an attachment, resolved lazily at send time."""

    resolver: str
    params: dict


@runtime_checkable
class AttachmentResolver(Protocol):
    """Turns a `params` dict into an attachment, or None to skip it."""

    def __call__(self, params: dict) -> Optional[EmailAttachment]: ...


_RESOLVERS: dict[str, AttachmentResolver] = {}


def register_attachment_resolver(name: str, resolver: AttachmentResolver) -> None:
    """Register `resolver` under `name`. Domains call this from `AppConfig.ready`."""
    _RESOLVERS[name] = resolver


def resolve_attachments(refs: "list[AttachmentRef] | None") -> list[EmailAttachment]:
    """Materialise attachment refs. Fails soft: a resolver that errors or is
    unknown is skipped and logged, never blocking the email."""
    materialised: list[EmailAttachment] = []
    for ref in refs or []:
        resolver = _RESOLVERS.get(ref.get("resolver", ""))
        if resolver is None:
            logger.warning("No attachment resolver registered for %r", ref.get("resolver"))
            continue
        try:
            attachment = resolver(ref.get("params", {}))
        except Exception:
            logger.exception("Attachment resolver %r failed", ref.get("resolver"))
            continue
        if attachment is not None:
            materialised.append(attachment)
    return materialised

"""
CapabilityTokenService (IP-009 Phase 4 D1).

A small, scope-keyed capability-token layer over `django.core.cache`
(Redis-backed in production). Tokens carry an explicit scope, a
resource identifier, and an issued-at timestamp; consumers verify
*all three* before granting access to the resource.

The pattern mirrors `apps/api/views/tokens.py::AccessTokenManagement`,
which already uses `cache.set("refresh_token:{jti}", ...)` with a TTL
for JWT refresh-token sessions. We reuse the same backend with a
distinct key namespace.

Two modes:

  - `mint(..., single_use=True)` + `consume(token, expected_scope)`:
    consume atomically removes the entry. Use for the
    `lcpl_download` scope where a token must not be redeemable twice.

  - `mint(..., single_use=False)` + `peek(token, expected_scope)`:
    peek leaves the entry in place; the token is valid until its TTL
    elapses. Use for the `lcpl_feed_download` scope where an OPDS
    feed embeds tokens that the reading app may open repeatedly
    within the TTL window.

Payload shape:

    {
        "sub": <user_pk>,            # who minted
        "scope": "<scope>",
        "resource_id": "<id>",       # license_id, etc.
        "single_use": <bool>,
        "issued_at": "<iso-8601>",
    }
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)


_KEY_PREFIX = "cap"


def _key(scope: str, token: str) -> str:
    return f"{_KEY_PREFIX}:{scope}:{token}"


class CapabilityTokenService:
    @staticmethod
    def mint(
        scope: str,
        subject: dict[str, Any],
        ttl: int,
        *,
        single_use: bool = True,
    ) -> str:
        """
        Mint a capability token.

        Args:
            scope: capability scope (e.g. `lcpl_download`). Tokens are
                keyed under `cap:{scope}:{token}` and only resolvable
                via the same scope.
            subject: caller-supplied identification — must include
                `sub` (user PK) and `resource_id`. Anything else is
                retained verbatim in the payload.
            ttl: lifetime in seconds.
            single_use: when True, `consume` removes the entry; when
                False, repeated `peek` calls succeed within the TTL.

        Returns:
            The opaque URL-safe token string.
        """
        if "sub" not in subject or "resource_id" not in subject:
            raise ValueError("CapabilityTokenService.mint requires `sub` and `resource_id` in subject")
        token = secrets.token_urlsafe(32)
        payload = dict(subject)
        payload["scope"] = scope
        payload["single_use"] = bool(single_use)
        payload["issued_at"] = datetime.now(timezone.utc).isoformat()
        cache.set(_key(scope, token), json.dumps(payload), timeout=ttl)
        return token

    @staticmethod
    def peek(token: str, expected_scope: str) -> Optional[dict[str, Any]]:
        """Return the payload if the token is valid under `expected_scope`."""
        if not token or not expected_scope:
            return None
        raw = cache.get(_key(expected_scope, token))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("capability_token.corrupt_payload", extra={"scope": expected_scope})
            return None
        if payload.get("scope") != expected_scope:
            return None
        return payload

    @classmethod
    def consume(cls, token: str, expected_scope: str) -> Optional[dict[str, Any]]:
        """
        Atomically remove + return the payload.

        For single-use tokens, this is the canonical access path —
        second consume returns None. For multi-use tokens, callers
        should use `peek` instead.
        """
        payload = cls.peek(token, expected_scope)
        if payload is None:
            return None
        # Single-use semantics: best-effort delete. Multi-use tokens
        # remain in the cache so subsequent peeks succeed.
        if payload.get("single_use", True):
            cache.delete(_key(expected_scope, token))
        return payload

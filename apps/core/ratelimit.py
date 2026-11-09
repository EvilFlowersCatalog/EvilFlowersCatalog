"""Request rate limits (#21, IP-026).

A fixed window per client, counted in the shared cache (Redis), so every gunicorn worker and every replica see the
same count. The client is:

- a signed-in user, by account — or by API key with `EVILFLOWERS_RATE_LIMIT_KEY=api_key`, so each integration
  of one account has its own budget;
- anyone else by IP address, taken from `X-Forwarded-For` at the position the trusted proxies append to
  (`EVILFLOWERS_RATE_LIMIT_PROXY_COUNT`), never from the leftmost entry a client can write itself.

Every limited response carries the W3C API style headers `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset` (epoch seconds); a refused one is `429` with `Retry-After`.

Not counted: the continuation requests of a ranged download (a PDF viewer fetches a book in dozens of
`Range: bytes=…` requests; the first one counts), views that opt out with `rate_limited = False`, and — by
default — superusers, whose bulk imports run through MCP. When the cache is unreachable the request is let
through: a rate limit must not become an outage.
"""

import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_PREFIX = "ratelimit"


@dataclass(frozen=True)
class Decision:
    limit: int
    remaining: int
    #: Epoch seconds when the window, and with it the count, resets.
    reset: int
    allowed: bool

    def headers(self) -> tuple:
        values = (
            ("X-RateLimit-Limit", str(self.limit)),
            ("X-RateLimit-Remaining", str(self.remaining)),
            ("X-RateLimit-Reset", str(self.reset)),
        )
        if not self.allowed:
            values += (("Retry-After", str(max(1, self.reset - int(time.time())))),)
        return values


def client_ip(request) -> str:
    """The client's address as the trusted proxies saw it.

    Each proxy appends the address it received the request from to `X-Forwarded-For`, so with N trusted proxies
    the Nth entry from the right is the client; anything left of it was written by the client and may be forged.
    With no trusted proxies, or a shorter header than expected, the socket's peer address is used.
    """
    proxies = settings.EVILFLOWERS_RATE_LIMIT_PROXY_COUNT
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    candidate = forwarded[-proxies] if proxies and len(forwarded) >= proxies else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.META.get("REMOTE_ADDR", "") or "unknown"


def _identity(request) -> Optional[tuple]:
    """`(key, limit)` for this request, or None when it is not limited."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        if user.is_superuser and settings.EVILFLOWERS_RATE_LIMIT_EXEMPT_SUPERUSERS:
            return None
        api_key = getattr(request, "api_key", None)
        if settings.EVILFLOWERS_RATE_LIMIT_KEY == "api_key" and api_key is not None:
            return f"key:{api_key.pk}", settings.EVILFLOWERS_RATE_LIMIT_AUTHENTICATED
        return f"user:{user.pk}", settings.EVILFLOWERS_RATE_LIMIT_AUTHENTICATED
    return f"ip:{client_ip(request)}", settings.EVILFLOWERS_RATE_LIMIT_ANONYMOUS


def _count(key: str, window: int) -> tuple:
    """`(requests so far in this window, epoch of the window's end)`, after counting this one."""
    start = int(time.time()) // window * window
    cache_key = f"{CACHE_PREFIX}:{key}:{start}"
    # `add` creates the counter only if it is missing; `incr` is atomic in Redis.
    cache.add(cache_key, 0, timeout=window + 60)
    try:
        count = cache.incr(cache_key)
    except ValueError:  # expired between add and incr
        cache.set(cache_key, 1, timeout=window + 60)
        count = 1
    return count, start + window


def _hit(key: str, limit: int) -> Optional[Decision]:
    window = settings.EVILFLOWERS_RATE_LIMIT_WINDOW
    try:
        count, reset = _count(key, window)
    except Exception:
        logger.warning("Rate limit cache unavailable; request let through", exc_info=True)
        return None
    return Decision(limit=limit, remaining=max(0, limit - count), reset=reset, allowed=count <= limit)


def check(request, view=None) -> Optional[Decision]:
    """Count this request against its client's budget. None when rate limits do not apply to it."""
    if not settings.EVILFLOWERS_RATE_LIMIT_ENABLED:
        return None
    if view is not None and not getattr(view, "rate_limited", True):
        return None
    if _is_range_continuation(request):
        return None
    identity = _identity(request)
    if identity is None:
        return None
    return _hit(*identity)


def check_failed_authentication(request) -> Optional[Decision]:
    """Count a request whose credentials were refused against its IP, so passwords cannot be guessed freely."""
    if not settings.EVILFLOWERS_RATE_LIMIT_ENABLED:
        return None
    return _hit(f"ip:{client_ip(request)}", settings.EVILFLOWERS_RATE_LIMIT_ANONYMOUS)


def _is_range_continuation(request) -> bool:
    from apps.files.ranges import is_continuation

    return is_continuation(request)

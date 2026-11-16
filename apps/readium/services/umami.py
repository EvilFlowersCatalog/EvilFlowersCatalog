"""
Portal visitors from Umami, read server side for `GET /api/v1/statistics` (#194).

The portal reports page views to Umami from the browser. Reading them back needs a credential, which must not
reach the browser, and a public share link would make the analytics world-readable, so the catalog fetches the
numbers and passes them through.

Which number: Umami's website stats endpoint (`GET /api/websites/{id}/stats?startAt&endAt`, epoch ms) returns
`visitors` = distinct sessions in that range. The page-views endpoint also returns a `sessions` series per time
unit, but a week or a month is not one of its units, and summing days counts a returning visitor once per day.
So this module makes one stats call per bucket. Note that an Umami "session" is a salted hash of the client that
rotates monthly, so "visitors" is distinct browsers, not distinct people.

Scope: Umami counts the whole portal. `visitors` is portal-wide even when the statistics are for one catalog.

Failure handling: the whole fetch has one time budget (`EVILFLOWERS_UMAMI_TIMEOUT`, default 2 s), calls run in
parallel, and any bucket without an answer in time is `None`. Nothing here raises. Past buckets never change and
are cached for a day; `None` is never cached.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

PAST_BUCKET_CACHE_SECONDS = 86400
TOKEN_CACHE_SECONDS = 3600
MAX_WORKERS = 8
_TOKEN_KEY = "umami:v1:token"


def configured() -> bool:
    return bool(
        settings.EVILFLOWERS_UMAMI_URL
        and settings.EVILFLOWERS_UMAMI_WEBSITE_ID
        and (
            settings.EVILFLOWERS_UMAMI_API_KEY
            or (settings.EVILFLOWERS_UMAMI_USERNAME and settings.EVILFLOWERS_UMAMI_PASSWORD)
        )
    )


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _cache_key(start: datetime, end: datetime) -> str:
    return f"umami:v1:visitors:{settings.EVILFLOWERS_UMAMI_WEBSITE_ID}:{_ms(start)}:{_ms(end)}"


def _headers(deadline: float) -> Optional[dict]:
    key = settings.EVILFLOWERS_UMAMI_API_KEY
    if key:
        # Self-hosted Umami takes an API key as a bearer token; Umami Cloud reads `x-umami-api-key`.
        return {"Authorization": f"Bearer {key}", "x-umami-api-key": key, "Accept": "application/json"}

    token = cache.get(_TOKEN_KEY)
    if not token:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        response = requests.post(
            f"{settings.EVILFLOWERS_UMAMI_URL}/api/auth/login",
            json={"username": settings.EVILFLOWERS_UMAMI_USERNAME, "password": settings.EVILFLOWERS_UMAMI_PASSWORD},
            timeout=remaining,
        )
        response.raise_for_status()
        token = response.json().get("token")
        if not token:
            # A 2FA account answers with `partialToken`: use a service account without 2FA, or an API key.
            raise ValueError("Umami login returned no token")
        cache.set(_TOKEN_KEY, token, TOKEN_CACHE_SECONDS)
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _parse_visitors(body) -> Optional[int]:
    """`visitors` as a number (Umami 2.x later releases and 3.x) or as `{value, prev}` (early 2.x)."""
    value = body.get("visitors") if isinstance(body, dict) else None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    return int(value)


def _fetch(headers: dict, start: datetime, end: datetime, timeout: float) -> Optional[int]:
    response = requests.get(
        f"{settings.EVILFLOWERS_UMAMI_URL}/api/websites/{settings.EVILFLOWERS_UMAMI_WEBSITE_ID}/stats",
        # Umami's range is inclusive at both ends; buckets are half-open `[start, end)`.
        params={"startAt": _ms(start), "endAt": _ms(end) - 1},
        headers=headers,
        timeout=timeout,
    )
    if response.status_code == 401:
        cache.delete(_TOKEN_KEY)
    response.raise_for_status()
    return _parse_visitors(response.json())


def visitors_per_window(windows: Sequence[Tuple[datetime, datetime]], now: datetime) -> List[Optional[int]]:
    """Distinct portal visitors for each `[start, end)`; `None` where Umami is off, down or too slow."""
    result: List[Optional[int]] = [None] * len(windows)
    if not windows or not configured():
        return result

    try:
        cached = cache.get_many([_cache_key(start, end) for start, end in windows])
    except Exception:
        cached = {}
    missing = []
    for i, (start, end) in enumerate(windows):
        value = cached.get(_cache_key(start, end))
        if value is not None:
            result[i] = value
        else:
            missing.append(i)
    if not missing:
        return result

    deadline = time.monotonic() + settings.EVILFLOWERS_UMAMI_TIMEOUT
    try:
        headers = _headers(deadline)
    except Exception:
        logger.warning(
            "statistics.umami_login_failed", extra={"event": "statistics.umami_login_failed"}, exc_info=True
        )
        return result
    if headers is None:
        return result

    executor = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(missing)), thread_name_prefix="umami")
    try:
        futures = {}
        for i in missing:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            start, end = windows[i]
            futures[executor.submit(_fetch, headers, start, end, remaining)] = i
        done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        failed = 0
        to_cache = {}
        for future in done:
            i = futures[future]
            try:
                value = future.result()
            except Exception:
                failed += 1
                continue
            result[i] = value
            start, end = windows[i]
            if value is not None and end <= now:
                to_cache[_cache_key(start, end)] = value
        if failed or len(done) < len(missing):
            logger.warning(
                "statistics.umami_incomplete",
                extra={
                    "event": "statistics.umami_incomplete",
                    "buckets": len(missing),
                    "answered": len(done) - failed,
                },
            )
        if to_cache:
            try:
                cache.set_many(to_cache, PAST_BUCKET_CACHE_SECONDS)
            except Exception:
                pass
    finally:
        # Never wait for a slow Umami: late answers are dropped.
        executor.shutdown(wait=False, cancel_futures=True)
    return result

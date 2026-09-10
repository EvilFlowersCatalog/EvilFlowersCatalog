"""
LSD HTTP transport (IP-009 Phase 3 C1).

Pure HTTP wrapper around the LCP Status Server. **No DB writes.**
`StatusServerSyncService` orchestrates this transport + the local
`License` row reconcile; this module just speaks HTTP.

Public surface:

    LsdTransport(url=None)
        .register(lcp_license)
        .get_status(lcp_license_id) -> dict
        .patch_status(lcp_license_id, payload) -> dict
        .post_register_device(lcp_license_id, device_id, device_name) -> dict
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Cap surfaced response bodies. Big enough to carry an RFC 7807 problem
# detail (title + detail + a few fields), small enough to keep Sentry
# tags and Django ProblemDetailException bodies sane.
_BODY_SNIPPET_LIMIT = 500


class LsdTransportError(Exception):
    """Wraps `requests.RequestException` with status/body context."""


def _redact(url: str) -> str:
    """Strip userinfo from a URL so it's safe to log/raise."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.username:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _format_error(label: str, e: requests.RequestException) -> str:
    """Build a one-line error string with status and body snippet.

    `label` should already include the service tag, e.g. "LSD patch_status"
    or "LCP generate_license", so logs are searchable.

    Includes:
      - HTTP status if available (otherwise the requests exception class)
      - redacted URL (no userinfo)
      - body snippet trimmed to _BODY_SNIPPET_LIMIT

    Also logs the full body at WARNING so the snippet doesn't lose
    information the truncated message dropped.
    """
    response = getattr(e, "response", None)
    url = _redact(getattr(response, "url", "") or getattr(getattr(e, "request", None), "url", "") or "")
    if response is None:
        return f"{label} failed [{type(e).__name__}] url={url}: {e}"

    body = response.text or ""
    snippet = body[:_BODY_SNIPPET_LIMIT]
    if len(body) > _BODY_SNIPPET_LIMIT:
        snippet += "…"
    logger.warning(
        "%s failed: status=%s url=%s body=%s",
        label,
        response.status_code,
        url,
        body,
    )
    return f"{label} failed: status={response.status_code} url={url} body={snippet!r}"


def _split_url_and_auth(url: str):
    """Extract Basic-auth credentials from a URL's userinfo.

    Returns (clean_url, (user, password) | None). Kept as a module helper
    so other Readium clients can reuse it. `requests` does pick up
    URL-embedded credentials automatically, but extracting them lets us
    (a) log a redacted URL and (b) keep an explicit auth path that's
    robust across redirects and string-concatenated endpoint paths.
    """
    parts = urlsplit(url)
    if not parts.username:
        return url, None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return clean, (parts.username, parts.password or "")


class LsdTransport:
    def __init__(self, url: Optional[str] = None, timeout: int = 30):
        raw_url = url or getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")
        self.url, self.auth = _split_url_and_auth(raw_url)
        self.timeout = timeout

    def register(self, lcp_license: Dict) -> None:
        """PUT /licenses — register a freshly-issued LCP license."""
        try:
            response = requests.put(
                f"{self.url}/licenses",
                json=lcp_license,
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD register", e)) from e

    def get_status(self, lcp_license_id) -> Dict:
        """GET /licenses/{id}/status — canonical status document."""
        try:
            response = requests.get(
                f"{self.url}/licenses/{lcp_license_id}/status",
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD get_status", e)) from e

    def put_return(self, lcp_license_id, device_id: str = "", device_name: str = "") -> Dict:
        """PUT /licenses/{id}/return — return a loan.

        Returning is its own LSD endpoint. It is emphatically *not*
        `PATCH /status`: that route is `LendingCancellation` upstream and
        rejects anything other than `cancelled`/`revoked` with a 400
        ("The new status must be either cancelled or revoked").

        Upstream derives the resulting status itself — READY becomes
        `cancelled`, ACTIVE/EXPIRED become `returned` — so callers should
        reconcile rather than assume.
        """
        try:
            response = requests.put(
                f"{self.url}/licenses/{lcp_license_id}/return",
                params={"id": device_id, "name": device_name},
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD return", e)) from e

    def put_renew(
        self,
        lcp_license_id,
        end: Optional[datetime] = None,
        device_id: str = "",
        device_name: str = "",
    ) -> Dict:
        """PUT /licenses/{id}/renew — extend a loan.

        Like `put_return`, a dedicated endpoint rather than a status PATCH.
        `end` is sent as RFC 3339, which is what upstream parses. Omitting
        it lets the Status Server apply its configured `renew_days`.

        Upstream refuses the renewal unless the status document carries a
        `potential_rights.end` upper bound, which it only sets when the
        Status Server config has `license_status.renew: true` and
        `renting_days > 0`.
        """
        params = {"id": device_id, "name": device_name}
        if end is not None:
            params["end"] = end.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            response = requests.put(
                f"{self.url}/licenses/{lcp_license_id}/renew",
                params=params,
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD renew", e)) from e

    def patch_status(self, lcp_license_id, payload: Dict) -> Dict:
        """PATCH /licenses/{id}/status — cancel or revoke.

        Upstream (`LendingCancellation`) accepts `cancelled` and `revoked`
        only. Use `put_return` / `put_renew` for the loan lifecycle.
        """
        try:
            response = requests.patch(
                f"{self.url}/licenses/{lcp_license_id}/status",
                json=payload,
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
            # PATCH may return empty body; tolerate both.
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD patch_status", e)) from e

    def post_register_device(self, lcp_license_id, device_id: str, device_name: str) -> Dict:
        """POST /licenses/{id}/register — device registration."""
        try:
            response = requests.post(
                f"{self.url}/licenses/{lcp_license_id}/register",
                params={"id": device_id, "name": device_name},
                timeout=self.timeout,
                auth=self.auth,
            )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(_format_error("LSD register_device", e)) from e

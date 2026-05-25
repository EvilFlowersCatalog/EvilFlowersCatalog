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

from typing import Dict, Optional

import requests
from django.conf import settings


class LsdTransportError(Exception):
    """Wraps `requests.RequestException` with context."""


class LsdTransport:
    def __init__(self, url: Optional[str] = None, timeout: int = 30):
        self.url = url or getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")
        self.timeout = timeout

    def register(self, lcp_license: Dict) -> None:
        """PUT /licenses — register a freshly-issued LCP license."""
        try:
            response = requests.put(f"{self.url}/licenses", json=lcp_license, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            raise LsdTransportError(f"LSD register failed: {e}") from e

    def get_status(self, lcp_license_id) -> Dict:
        """GET /licenses/{id}/status — canonical status document."""
        try:
            response = requests.get(
                f"{self.url}/licenses/{lcp_license_id}/status",
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise LsdTransportError(f"LSD get_status failed: {e}") from e

    def patch_status(self, lcp_license_id, payload: Dict) -> Dict:
        """PATCH /licenses/{id}/status — request a status transition."""
        try:
            response = requests.patch(
                f"{self.url}/licenses/{lcp_license_id}/status",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            # PATCH may return empty body; tolerate both.
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(f"LSD patch_status failed: {e}") from e

    def post_register_device(self, lcp_license_id, device_id: str, device_name: str) -> Dict:
        """POST /licenses/{id}/register — device registration."""
        try:
            response = requests.post(
                f"{self.url}/licenses/{lcp_license_id}/register",
                params={"id": device_id, "name": device_name},
                timeout=self.timeout,
            )
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except requests.RequestException as e:
            raise LsdTransportError(f"LSD register_device failed: {e}") from e

"""
Backwards-compatible shim around `StatusServerSyncService`
(IP-009 Phase 3).

Pre-IP-009 callers `from apps.readium.services import StatusServerClient`
and invoke its methods directly. The class is preserved to avoid a
big-bang rewrite of every callsite, but each method now delegates to
`StatusServerSyncService` so the read-after-write reconcile semantics
(IP-008 Q1) are applied unconditionally.

New code should depend on `StatusServerSyncService` directly. This
shim will be removed after the in-tree call sites have migrated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from apps.readium.models import License
from .status_server_sync import StatusServerSyncService


class StatusServerClient:
    def __init__(self):
        self._sync = StatusServerSyncService()

    @property
    def status_server_url(self) -> str:
        # Exposed for tests / debugging only; transport owns the URL.
        return self._sync.transport.url

    def register_license(self, lcp_license: Dict) -> None:
        self._sync.register_license(lcp_license)

    def revoke_license(self, license: License, reason: str = "Revoked by administrator") -> None:
        self._sync.revoke_license(license, reason)

    def return_license(self, license: License) -> None:
        self._sync.return_license(license)

    def renew_license(self, license: License, new_end_date: datetime) -> None:
        self._sync.renew_license(license, new_end_date)

    def cancel_license(self, license: License, reason: str = "Cancelled") -> None:
        self._sync.cancel_license(license, reason)

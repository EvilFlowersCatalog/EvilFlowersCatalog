"""
LSD lifecycle sync service (IP-009 Phase 3, IP-008 Q1 resolution).

The Status Server is the canonical surface for license state. The
local `License` row is a cache of the LSD document. Every catalog-
driven mutation through this service follows:

    1. PATCH the Status Server with the requested transition.
    2. GET the canonical status document back.
    3. Reconcile `License.state`, `License.device_count`,
       `License.expires_at` from the GET response.

A GET failure after a successful PATCH falls back to an optimistic
write of the *expected* post-PATCH state and logs a divergence
metric — better to commit an expected value than to leave the row
silently out of sync.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.readium.models import License
from .lsd_transport import LsdTransport, LsdTransportError

logger = logging.getLogger(__name__)


# LSD status string → local LicenseState enum value.
_LSD_STATUS_TO_STATE = {
    "ready": License.LicenseState.READY,
    "active": License.LicenseState.ACTIVE,
    "returned": License.LicenseState.RETURNED,
    "revoked": License.LicenseState.REVOKED,
    "cancelled": License.LicenseState.CANCELLED,
    "expired": License.LicenseState.EXPIRED,
}


class StatusServerSyncService:
    def __init__(self, transport: Optional[LsdTransport] = None):
        self.transport = transport or LsdTransport()

    # ----- public mutation methods ------------------------------------

    def register_license(self, lcp_license: Dict) -> None:
        """Forward a freshly-issued LCP license to the LSD."""
        self.transport.register(lcp_license)

    def return_license(self, license: License) -> License:
        return self._patch_then_reconcile(
            license,
            patch_payload={"status": "returned"},
            expected_state=License.LicenseState.RETURNED,
            expected_expires_at=timezone.now(),
        )

    def renew_license(self, license: License, new_end_date: datetime) -> License:
        return self._patch_then_reconcile(
            license,
            patch_payload={"status": "active", "end": new_end_date.isoformat()},
            expected_state=License.LicenseState.ACTIVE,
            expected_expires_at=new_end_date,
        )

    def revoke_license(self, license: License, reason: str = "Revoked by administrator") -> License:
        return self._patch_then_reconcile(
            license,
            patch_payload={"status": "revoked", "message": reason},
            expected_state=License.LicenseState.REVOKED,
        )

    def cancel_license(self, license: License, reason: str = "Cancelled by user") -> License:
        return self._patch_then_reconcile(
            license,
            patch_payload={"status": "cancelled", "message": reason},
            expected_state=License.LicenseState.CANCELLED,
        )

    def register_device(self, license: License, device_id: str, device_name: str) -> License:
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")
        self.transport.post_register_device(license.lcp_license_id, device_id, device_name)
        # Device registrations don't change `License.state` other than
        # READY → ACTIVE; reconcile from the canonical doc.
        return self.reconcile(license)

    # ----- reconciliation --------------------------------------------

    def reconcile(self, license: License, lsd_doc: Optional[Dict] = None) -> License:
        """
        Refetch the canonical LSD doc and apply it to the local row.

        Callers can pass a pre-fetched `lsd_doc` (e.g. the response to
        a proxied PUT in `status_proxy.py`) to skip the network
        round-trip.
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")
        if lsd_doc is None:
            try:
                lsd_doc = self.transport.get_status(license.lcp_license_id)
            except LsdTransportError:
                logger.exception("lsd.reconcile.get_failed", extra={"license_id": str(license.pk)})
                return license
        self._apply_lsd_document(license, lsd_doc)
        return license

    # ----- internals --------------------------------------------------

    def _patch_then_reconcile(
        self,
        license: License,
        patch_payload: Dict,
        expected_state: str,
        expected_expires_at: Optional[datetime] = None,
    ) -> License:
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        # 1. PATCH the canonical state surface first.
        try:
            self.transport.patch_status(license.lcp_license_id, patch_payload)
        except LsdTransportError:
            # PATCH failed: do NOT write locally. Surface upstream.
            logger.exception(
                "lsd.patch.failed",
                extra={"license_id": str(license.pk), "payload": patch_payload},
            )
            raise

        # 2. GET the canonical document. On failure, fall back to the
        # optimistic local write so the row reflects the requested
        # transition.
        try:
            doc = self.transport.get_status(license.lcp_license_id)
            self._apply_lsd_document(license, doc)
        except LsdTransportError:
            logger.warning(
                "lsd.reconcile.fallback",
                extra={"license_id": str(license.pk), "expected_state": expected_state},
            )
            license.state = expected_state
            if expected_expires_at is not None:
                license.expires_at = expected_expires_at
            license.save(update_fields=["state", "expires_at", "updated_at"])

        # 3. Update LCP rights on return / renew so /fresh reflects the
        # new end date.
        if expected_expires_at is not None and expected_state in (
            License.LicenseState.RETURNED,
            License.LicenseState.ACTIVE,
        ):
            from .lcp_server_client import LCPServerClient

            try:
                LCPServerClient().update_license_rights(license)
            except Exception:
                logger.exception(
                    "lcp.update_rights.failed",
                    extra={"license_id": str(license.pk)},
                )

        return license

    @staticmethod
    def _apply_lsd_document(license: License, doc: Dict) -> None:
        """
        Map the LSD document onto the local License row.

        - `doc["status"]`            → `License.state`
        - `doc["potential_rights"]["end"]` or `doc["updated"]["end"]`
                                     → `License.expires_at`
        - aggregate of `doc["events"]` where `type=register` minus
          `type=return` → `License.device_count` (clamped ≥ 0)
        """
        update_fields = ["updated_at"]
        status = doc.get("status")
        if status and status in _LSD_STATUS_TO_STATE:
            new_state = _LSD_STATUS_TO_STATE[status]
            if license.state != new_state:
                license.state = new_state
                update_fields.append("state")

        # Some LSD implementations stash the new end under
        # `potential_rights.end`, others under `updated.end`. Try both.
        new_end_raw = None
        potential = doc.get("potential_rights") or {}
        if isinstance(potential, dict):
            new_end_raw = potential.get("end")
        if not new_end_raw:
            updated = doc.get("updated") or {}
            if isinstance(updated, dict):
                new_end_raw = updated.get("end")
        if new_end_raw:
            parsed = parse_datetime(new_end_raw) if isinstance(new_end_raw, str) else new_end_raw
            if parsed is not None and license.expires_at != parsed:
                license.expires_at = parsed
                update_fields.append("expires_at")

        events = doc.get("events") or []
        if isinstance(events, list) and events:
            registrations = sum(1 for e in events if isinstance(e, dict) and e.get("type") == "register")
            returns = sum(1 for e in events if isinstance(e, dict) and e.get("type") == "return")
            device_count = max(0, registrations - returns)
            if license.device_count != device_count:
                license.device_count = device_count
                update_fields.append("device_count")

        if len(update_fields) > 1:  # something beyond `updated_at` changed
            license.save(update_fields=update_fields)

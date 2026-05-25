"""
LCP Status Server Client

Handles communication with the LCP Status Server (LSD).
Responsible for license status management, device tracking, and returns/renewals.
"""

import requests
from typing import Dict
from django.conf import settings
from django.utils import timezone
from datetime import datetime

from apps.readium.models import License


class StatusServerClient:
    """
    Client for communicating with LCP Status Server (LSD).

    The Status Server is responsible for:
    - Registering licenses (making them available for status checks)
    - Tracking license status (active, returned, revoked, etc.)
    - Managing device registrations
    - Handling returns and renewals
    """

    def __init__(self):
        self.status_server_url = getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")

    def register_license(self, lcp_license: Dict) -> None:
        """
        Register a license with the Status Server.

        This should be called after generating a new license with the License Server.
        It makes the license available for status tracking and device registration.

        Args:
            lcp_license: Complete LCP license JSON from License Server

        Raises:
            Exception: If Status Server returns error
        """
        try:
            # PUT /licenses to register new license
            response = requests.put(
                f"{self.status_server_url}/licenses",
                json=lcp_license,
                timeout=30,
            )
            response.raise_for_status()

        except requests.RequestException as e:
            raise Exception(f"Failed to register license with status server: {str(e)}")

    def revoke_license(self, license: License, reason: str = "Revoked by administrator") -> None:
        """
        Revoke a license through the Status Server.

        Once revoked, reading apps will detect this status and prevent opening the content.
        This is permanent and cannot be undone.

        Args:
            license: License model instance to revoke
            reason: Human-readable reason for revocation

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If Status Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        status_update = {"status": "revoked", "message": reason}

        try:
            # PATCH /licenses/{license_id}/status
            response = requests.patch(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status",
                json=status_update,
                timeout=30,
            )
            response.raise_for_status()

            # Update local license state
            license.state = License.LicenseState.REVOKED
            license.save()

        except requests.RequestException as e:
            raise Exception(f"Failed to revoke license: {str(e)}")

    def return_license(self, license: License) -> None:
        """
        Return a license (end it immediately).

        Three-step flow (IP-008 Phase 1 A4):
        1. PATCH /licenses/{id}/status on the Status Server with
           status=returned.
        2. Update rights on the LCP License Server (rights.end = now)
           so subsequent /fresh requests reflect the new end date.
        3. Persist local state. The local License row is a cache; the
           Status Server is the canonical state surface per LSD §5.1.

        Args:
            license: License model instance to return

        Raises:
            Exception: If update fails
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        # 1. PATCH Status Server first — canonical state surface.
        try:
            response = requests.patch(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status",
                json={"status": "returned"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise Exception(f"Failed to return license on status server: {str(e)}")

        # 2. Update rights on LCP License Server.
        license.expires_at = timezone.now()
        from .lcp_server_client import LCPServerClient

        LCPServerClient().update_license_rights(license)

        # 3. Persist local state as cache of the canonical LSD value.
        license.state = License.LicenseState.RETURNED
        license.save()

    def renew_license(self, license: License, new_end_date: datetime) -> None:
        """
        Renew a license with a new end date.

        Three-step flow (IP-008 Phase 1 A4):
        1. PATCH /licenses/{id}/status on the Status Server with
           status=active and the new end date.
        2. Update rights on the LCP License Server (rights.end =
           new_end_date) so /fresh reflects the extension.
        3. Persist local state.

        Args:
            license: License model instance to renew
            new_end_date: New expiration datetime

        Raises:
            Exception: If update fails
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        # 1. PATCH Status Server first.
        try:
            response = requests.patch(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status",
                json={"status": "active", "end": new_end_date.isoformat()},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise Exception(f"Failed to renew license on status server: {str(e)}")

        # 2. Update rights on LCP License Server.
        license.expires_at = new_end_date
        from .lcp_server_client import LCPServerClient

        LCPServerClient().update_license_rights(license)

        # 3. Persist local state.
        license.save()

    def cancel_license(self, license: License, reason: str = "Cancelled") -> None:
        """
        Cancel a license through the Status Server.

        Similar to revoke but typically used for user-initiated cancellations.

        Args:
            license: License model instance to cancel
            reason: Human-readable reason for cancellation

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If Status Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        status_update = {"status": "cancelled", "message": reason}

        try:
            # PATCH /licenses/{license_id}/status
            response = requests.patch(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status",
                json=status_update,
                timeout=30,
            )
            response.raise_for_status()

            # Update local license state
            license.state = License.LicenseState.CANCELLED
            license.save()

        except requests.RequestException as e:
            raise Exception(f"Failed to cancel license: {str(e)}")

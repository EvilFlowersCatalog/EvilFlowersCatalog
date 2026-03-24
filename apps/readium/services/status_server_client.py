"""
LCP Status Server Client

Handles communication with the LCP Status Server (LSD).
Responsible for license status management, device tracking, and returns/renewals.
"""

import requests
from typing import Dict, List
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
    - Detecting overshared licenses
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

        Sets the license end date to now and updates the License Server rights.
        This makes the license slot available for other users immediately.

        Args:
            license: License model instance to return

        Raises:
            Exception: If update fails
        """
        # Update license end date to now
        license.expires_at = timezone.now()
        license.state = License.LicenseState.RETURNED
        license.save()

        # Import here to avoid circular dependency
        from .lcp_server_client import LCPServerClient

        # Update rights on LCP License Server
        lcp_client = LCPServerClient()
        lcp_client.update_license_rights(license)

        # Note: Status Server will detect the updated end date when reading app
        # checks status next time. No separate Status Server call needed.

    def renew_license(self, license: License, new_end_date: datetime) -> None:
        """
        Renew a license with a new end date.

        Extends the license validity period and updates the License Server.

        Args:
            license: License model instance to renew
            new_end_date: New expiration datetime

        Raises:
            Exception: If update fails
        """
        # Update license end date
        license.expires_at = new_end_date
        license.save()

        # Import here to avoid circular dependency
        from .lcp_server_client import LCPServerClient

        # Update rights on LCP License Server
        lcp_client = LCPServerClient()
        lcp_client.update_license_rights(license)

        # Note: Status Server will detect the updated end date when reading app
        # checks status next time. No separate Status Server call needed.

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

    def get_license_status(self, license: License) -> Dict:
        """
        Get current license status from Status Server.

        Returns information about license state, device registrations, events, etc.

        Args:
            license: License model instance

        Returns:
            License status JSON from Status Server

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If Status Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        try:
            # GET /licenses/{license_id}/status
            response = requests.get(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status",
                timeout=30,
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to get license status: {str(e)}")

    def get_registered_devices(self, license: License) -> List[Dict]:
        """
        Get list of devices registered for this license.

        Returns information about devices that have opened this content.

        Args:
            license: License model instance

        Returns:
            List of registered device info

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If Status Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        try:
            # GET /licenses/{license_id}/registered
            response = requests.get(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/registered",
                timeout=30,
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to get registered devices: {str(e)}")

    def check_overshared_licenses(self, device_threshold: int = 5) -> List[Dict]:
        """
        Check for overshared licenses (licenses with many devices).

        Useful for detecting potential license abuse or sharing.

        Args:
            device_threshold: Minimum number of devices to flag as overshared

        Returns:
            List of overshared license info

        Raises:
            Exception: If Status Server returns error
        """
        try:
            # GET /licenses?devices={threshold}
            response = requests.get(
                f"{self.status_server_url}/licenses",
                params={"devices": device_threshold},
                timeout=30,
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to check overshared licenses: {str(e)}")

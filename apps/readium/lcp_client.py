import hashlib
import requests
from typing import Dict
from django.conf import settings
from django.utils import timezone
from datetime import datetime

from apps.readium.models import License


class LCPClient:
    """Client for interacting with LCP License and Status servers"""

    def __init__(self):
        self.license_server_url = settings.EVILFLOWERS_READIUM_LCPSV_URL
        self.status_server_url = getattr(settings, "EVILFLOWERS_READIUM_LSDSV_URL", "http://127.0.0.1:8990")
        self.provider_url = getattr(settings, "EVILFLOWERS_READIUM_PROVIDER_URL", "http://127.0.0.1:8000")

    def hash_passphrase(self, passphrase: str) -> str:
        """Hash a passphrase using SHA256"""
        return hashlib.sha256(passphrase.encode("utf-8")).hexdigest().upper()

    def generate_license(self, license: License, user_passphrase: str) -> Dict:
        """
        Generate a new LCP license by calling the License Server
        """
        # Hash the user passphrase
        passphrase_hash = self.hash_passphrase(user_passphrase)

        # Update license with passphrase hash
        license.passphrase_hash = passphrase_hash
        license.save()

        # Prepare partial license payload
        partial_license = {
            "provider": self.provider_url,
            "user": {
                "id": str(license.user.pk),
                "email": license.user.email,
                "name": license.user.get_full_name() or license.user.username,
                "encrypted": ["email"] if license.user.email else [],
            },
            "encryption": {
                "user_key": {
                    "text_hint": license.passphrase_hint or "Your library password",
                    "hex_value": passphrase_hash,
                }
            },
            "rights": {
                "start": license.starts_at.isoformat(),
                "end": license.expires_at.isoformat(),
                "print": 10,  # Allow 10 pages to be printed
                "copy": 2048,  # Allow 2048 characters to be copied
            },
        }

        # Call LCP License Server
        try:
            response = requests.post(
                f"{self.license_server_url}/contents/{license.content_id}/license", json=partial_license, timeout=30
            )
            response.raise_for_status()

            lcp_license = response.json()

            # Update license with LCP license ID
            license.lcp_license_id = lcp_license.get("id")
            license.state = License.LicenseState.READY
            license.save()

            return lcp_license

        except requests.RequestException as e:
            raise Exception(f"Failed to generate LCP license: {str(e)}")

    def fetch_fresh_license(self, license: License) -> Dict:
        """
        Fetch a fresh copy of an existing license
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        # Prepare partial license payload for fresh fetch
        partial_license = {
            "user": {
                "email": license.user.email,
                "name": license.user.get_full_name() or license.user.username,
                "encrypted": ["email"] if license.user.email else [],
            },
            "encryption": {
                "user_key": {
                    "text_hint": license.passphrase_hint or "Your library password",
                    "hex_value": license.passphrase_hash,
                }
            },
        }

        try:
            response = requests.post(
                f"{self.license_server_url}/licenses/{license.lcp_license_id}", json=partial_license, timeout=30
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to fetch fresh license: {str(e)}")

    def notify_status_server(self, lcp_license: Dict) -> None:
        """
        Notify the Status Server about a new license
        """
        try:
            response = requests.put(f"{self.status_server_url}/licenses", json=lcp_license, timeout=30)
            response.raise_for_status()

        except requests.RequestException as e:
            raise Exception(f"Failed to notify status server: {str(e)}")

    def update_license_rights(self, license: License) -> None:
        """
        Update license rights (used for returns, renewals)
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        partial_license = {
            "rights": {
                "start": license.starts_at.isoformat(),
                "end": license.expires_at.isoformat(),
                "print": 10,
                "copy": 2048,
            }
        }

        try:
            response = requests.patch(
                f"{self.license_server_url}/licenses/{license.lcp_license_id}", json=partial_license, timeout=30
            )
            response.raise_for_status()

        except requests.RequestException as e:
            raise Exception(f"Failed to update license rights: {str(e)}")

    def revoke_license(self, license: License, reason: str = "Revoked by administrator") -> None:
        """
        Revoke a license through the Status Server
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        status_update = {"status": "revoked", "message": reason}

        try:
            response = requests.patch(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/status", json=status_update, timeout=30
            )
            response.raise_for_status()

            # Update local license state
            license.state = License.LicenseState.REVOKED
            license.save()

        except requests.RequestException as e:
            raise Exception(f"Failed to revoke license: {str(e)}")

    def return_license(self, license: License) -> None:
        """
        Return a license (set end date to now)
        """
        # Update license end date
        license.expires_at = timezone.now()
        license.state = License.LicenseState.RETURNED
        license.save()

        # Update rights on LCP server
        self.update_license_rights(license)

    def renew_license(self, license: License, new_end_date: datetime) -> None:
        """
        Renew a license with a new end date
        """
        # Update license end date
        license.expires_at = new_end_date
        license.save()

        # Update rights on LCP server
        self.update_license_rights(license)

    def check_overshared_licenses(self, device_threshold: int = 5) -> list:
        """
        Check for overshared licenses from the Status Server
        """
        try:
            response = requests.get(
                f"{self.status_server_url}/licenses", params={"devices": device_threshold}, timeout=30
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to check overshared licenses: {str(e)}")

    def get_license_registered_devices(self, license: License) -> list:
        """
        Get registered devices for a license
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        try:
            response = requests.get(
                f"{self.status_server_url}/licenses/{license.lcp_license_id}/registered", timeout=30
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to get registered devices: {str(e)}")

"""
LCP License Server Client

Handles communication with the LCP License Server (lcpsv).
Responsible for license generation, fetching, and updates.
"""

import hashlib
import requests
from typing import Dict
from django.conf import settings

from apps.readium.models import License


class LCPServerClient:
    """
    Client for communicating with LCP License Server.

    The License Server is responsible for:
    - Generating new LCP licenses
    - Storing encrypted content metadata
    - Providing fresh licenses on request
    - Updating license rights (dates, print/copy limits)
    """

    def __init__(self):
        self.license_server_url = settings.EVILFLOWERS_READIUM_LCPSV_URL
        self.provider_url = getattr(settings, "EVILFLOWERS_READIUM_PROVIDER_URL", settings.EVILFLOWERS_BASE_URL)

    @staticmethod
    def hash_passphrase(passphrase: str) -> str:
        """
        Hash a passphrase using SHA-256.

        This creates the User Key that will be stored in the license.
        The reading app will hash the user's entered passphrase and compare.

        Args:
            passphrase: Plain text passphrase from user

        Returns:
            Uppercase hex string of SHA-256 hash
        """
        return hashlib.sha256(passphrase.encode("utf-8")).hexdigest().upper()

    def generate_license(
        self, license: License, user_passphrase: str, print_limit: int = 10, copy_limit: int = 2048
    ) -> Dict:
        """
        Generate a new LCP license by calling the License Server.

        This creates a license tied to:
        - The encrypted content (via lcp_content_id)
        - The specific user (with their passphrase)
        - Rights/restrictions (dates, print/copy limits)

        Args:
            license: License model instance (should have encrypted_content set)
            user_passphrase: User's chosen passphrase (will be hashed). Either this or passphrase_hash must be provided.
            passphrase_hash: Pre-computed SHA-256 hash of passphrase. Either this or user_passphrase must be provided.
            print_limit: Number of pages allowed to print (default: 10)
            copy_limit: Number of characters allowed to copy (default: 2048)

        Returns:
            Complete LCP license as JSON dict

        Raises:
            ValueError: If license missing encrypted_content or if neither passphrase nor hash provided
            Exception: If LCP Server returns error
        """
        if not license.encrypted_content:
            raise ValueError("License must have encrypted_content before generating")

        # Handle passphrase: use provided hash or hash the plain passphrase
        if passphrase_hash is not None:
            # Use pre-computed hash (uppercase for LCP spec)
            final_hash = passphrase_hash.upper()
        elif user_passphrase is not None:
            # Hash the user passphrase
            final_hash = self.hash_passphrase(user_passphrase)
        else:
            raise ValueError("Either user_passphrase or passphrase_hash must be provided")

        # Update license with passphrase hash
        license.passphrase_hash = final_hash
        license.save()

        # Prepare partial license payload per LCP spec
        partial_license = {
            "provider": self.provider_url,
            "user": {
                "id": str(license.user.pk),
                "email": getattr(license.user, "email", "") or "",
                "name": license.user.full_name or license.user.username,
                "encrypted": ["email"] if getattr(license.user, "email", "") else [],
            },
            "encryption": {
                "user_key": {
                    "text_hint": license.passphrase_hint or "Your library password",
                    "hex_value": final_hash,
                }
            },
            "rights": {
                "start": license.starts_at.isoformat(),
                "end": license.expires_at.isoformat(),
                "print": print_limit,
                "copy": copy_limit,
            },
        }

        # Call LCP License Server: POST /contents/{content_id}/license
        lcp_content_id = license.encrypted_content.lcp_content_id
        try:
            response = requests.post(
                f"{self.license_server_url}/contents/{lcp_content_id}/license",
                json=partial_license,
                timeout=30,
            )
            response.raise_for_status()

            lcp_license = response.json()

            # Update license with LCP license ID from server
            license.lcp_license_id = lcp_license.get("id")
            license.state = License.LicenseState.READY
            license.save()

            return lcp_license

        except requests.RequestException as e:
            raise Exception(f"Failed to generate LCP license: {str(e)}")

    def fetch_fresh_license(self, license: License) -> Dict:
        """
        Fetch a fresh copy of an existing license from LCP Server.

        This is used by the License Gateway endpoint when reading apps
        request the .lcpl file. It ensures they get the most up-to-date
        license with current rights/restrictions.

        Args:
            license: License model instance with lcp_license_id

        Returns:
            Complete LCP license as JSON dict

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If LCP Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        # Prepare partial license payload for fresh fetch
        # Must include user and encryption info to rebuild license
        partial_license = {
            "user": {
                "id": str(license.user.pk),
                "email": getattr(license.user, "email", "") or "",
                "name": license.user.full_name or license.user.username,
                "encrypted": ["email"] if getattr(license.user, "email", "") else [],
            },
            "encryption": {
                "user_key": {
                    "text_hint": license.passphrase_hint or "Your library password",
                    "hex_value": license.passphrase_hash,
                }
            },
        }

        try:
            # POST /licenses/{license_id} to get fresh license
            response = requests.post(
                f"{self.license_server_url}/licenses/{license.lcp_license_id}",
                json=partial_license,
                timeout=30,
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise Exception(f"Failed to fetch fresh license: {str(e)}")

    def update_license_rights(self, license: License, print_limit: int = 10, copy_limit: int = 2048) -> None:
        """
        Update license rights on LCP Server.

        Used when license is renewed (new end date) or returned (end date set to now).
        Updates the rights section of the license without changing other fields.

        Args:
            license: License model instance with updated dates
            print_limit: Number of pages allowed to print
            copy_limit: Number of characters allowed to copy

        Raises:
            ValueError: If license missing lcp_license_id
            Exception: If LCP Server returns error
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        partial_license = {
            "rights": {
                "start": license.starts_at.isoformat(),
                "end": license.expires_at.isoformat(),
                "print": print_limit,
                "copy": copy_limit,
            }
        }

        try:
            # PATCH /licenses/{license_id} to update rights
            response = requests.patch(
                f"{self.license_server_url}/licenses/{license.lcp_license_id}",
                json=partial_license,
                timeout=30,
            )
            response.raise_for_status()

        except requests.RequestException as e:
            raise Exception(f"Failed to update license rights: {str(e)}")

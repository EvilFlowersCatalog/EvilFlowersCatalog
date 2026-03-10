"""
License Service

Manages the complete license lifecycle including:
- Availability checking
- License creation
- Renewal and returns
- Revocation
"""

from datetime import datetime, timedelta
from typing import Dict, Optional
from django.db.models import Q, Count
from django.utils import timezone
from django.conf import settings
import uuid

from apps.core.models import Entry, User, Acquisition
from apps.readium.models import License, EncryptedContent
from .content_encryption_service import ContentEncryptionService
from .lcp_server_client import LCPServerClient
from .status_server_client import StatusServerClient


class LicenseService:
    """
    Service for managing license lifecycle.

    Handles:
    - Availability checking (calendar-based)
    - License creation with LCP integration
    - Renewal, return, revocation
    - Fetching fresh licenses for reading apps
    """

    @staticmethod
    def get_entry_availability(entry: Entry, start_date: datetime = None, end_date: datetime = None) -> Dict:
        """
        Get availability information for an entry over a date range.

        Returns calendar data showing available slots per day.
        Useful for frontend display of availability.

        Args:
            entry: Entry to check availability for
            start_date: Start of date range (default: now)
            end_date: End of date range (default: 90 days from start)

        Returns:
            Dict with:
                - available: bool - any availability in range
                - max_concurrent: int - max concurrent licenses
                - calendar: list - per-day availability data
        """
        if not entry.read_config("readium_enabled"):
            return {
                "available": False,
                "reason": "Entry is not readium-enabled",
                "calendar": [],
            }

        max_concurrent = entry.read_config("readium_amount")

        if start_date is None:
            start_date = timezone.now()
        if end_date is None:
            end_date = start_date + timedelta(days=90)  # Default 3 months

        # Get all active licenses for this entry in the date range
        active_licenses = License.objects.filter(
            entry=entry,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            starts_at__lte=end_date,
            expires_at__gte=start_date,
        ).order_by("starts_at")

        # Generate calendar data
        calendar = []
        current_date = start_date.date()
        end_date_only = end_date.date()

        while current_date <= end_date_only:
            # Count active licenses for this day
            day_licenses = active_licenses.filter(
                starts_at__date__lte=current_date, expires_at__date__gte=current_date
            ).count()

            available_slots = max_concurrent - day_licenses

            calendar.append(
                {
                    "date": current_date.isoformat(),
                    "available_slots": max(0, available_slots),
                    "total_slots": max_concurrent,
                    "is_available": available_slots > 0,
                }
            )

            current_date += timedelta(days=1)

        return {
            "available": any(day["is_available"] for day in calendar),
            "max_concurrent": max_concurrent,
            "calendar": calendar,
        }

    @staticmethod
    def can_user_borrow(
        entry: Entry,
        user: User,
        start_date: datetime = None,
        end_date: datetime = None,
    ) -> Dict:
        """
        Check if a user can borrow an entry for a specific period.

        Args:
            entry: Entry to borrow
            user: User requesting borrow
            start_date: Borrow start date (default: now)
            end_date: Borrow end date (default: 14 days from start)

        Returns:
            Dict with:
                - can_borrow: bool
                - reason: str (if can_borrow is False)
                - available_slots: int (if can_borrow is True)
        """
        if not entry.read_config("readium_enabled"):
            return {"can_borrow": False, "reason": "Entry is not readium-enabled"}

        if start_date is None:
            start_date = timezone.now()
        if end_date is None:
            end_date = start_date + timedelta(days=14)  # Default 2 weeks

        # Check if user already has an active license for this entry
        existing_license = License.objects.filter(
            entry=entry,
            user=user,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
        ).first()

        if existing_license:
            return {
                "can_borrow": False,
                "reason": "User already has an active license for this entry",
                "existing_license": existing_license.pk,
            }

        max_concurrent = entry.read_config("readium_amount")

        # Check availability for the requested period
        conflicting_licenses = License.objects.filter(
            entry=entry,
            state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
            starts_at__lt=end_date,
            expires_at__gt=start_date,
        ).count()

        if conflicting_licenses >= max_concurrent:
            return {
                "can_borrow": False,
                "reason": "No available slots for the requested period",
                "available_slots": max_concurrent - conflicting_licenses,
            }

        return {
            "can_borrow": True,
            "available_slots": max_concurrent - conflicting_licenses,
        }

    @staticmethod
    def create_license(
        entry: Entry,
        user: User,
        user_passphrase: Optional[str] = None,
        passphrase_hint: Optional[str] = None,
        start_date: datetime = None,
        duration_days: int = 14,
        print_limit: int = 10,
        copy_limit: int = 2048,
    ) -> License:
        """
        Create a new license for a user with LCP integration.

        This is the main entry point for license creation. It:
        1. Validates availability
        2. Ensures content is encrypted and registered
        3. Creates License record
        4. Generates LCP license via License Server
        5. Registers with Status Server

        Args:
            entry: Entry to license
            user: User receiving license
            user_passphrase: Optional passphrase for this license. If not provided, uses user's default passphrase.
            passphrase_hint: Optional hint for passphrase. If not provided, uses user's default hint.
            start_date: License start date (default: now)
            duration_days: License duration in days (default: 14)
            print_limit: Max pages to print (default: 10)
            copy_limit: Max characters to copy (default: 2048)

        Returns:
            License: Created license with LCP license ID

        Raises:
            ValueError: If validation fails or content not ready, or if user has no default passphrase
        """
        if start_date is None:
            start_date = timezone.now()

        end_date = start_date + timedelta(days=duration_days)

        # Handle passphrase: use provided or user's default
        if user_passphrase is None:
            if not user.lcp_passphrase_hash:
                raise ValueError("No LCP passphrase available. Please set your default passphrase")
            passphrase_hash = user.lcp_passphrase_hash
            # Use user's default hint if no custom hint provided
            if passphrase_hint is None:
                passphrase_hint = user.lcp_passphrase_hint
        else:
            # Hash the provided passphrase (uppercase for LCP spec compliance)
            passphrase_hash = LCPServerClient.hash_passphrase(user_passphrase)

        # Validate availability
        availability = LicenseService.can_user_borrow(entry, user, start_date, end_date)
        if not availability["can_borrow"]:
            raise ValueError(f"Cannot create license: {availability['reason']}")

        # Get the entry's acquisition (should be only one for readium entries)
        acquisition = entry.acquisitions.filter(entry=entry, acquisition_type="epub").first()
        if not acquisition:
            raise ValueError("Entry has no EPUB or PDF acquisition suitable for LCP protection")

        # Ensure content is encrypted
        if not hasattr(acquisition, "encrypted_content"):
            raise ValueError("Content not encrypted. Trigger encryption first via ContentEncryptionService.")

        encrypted_content = acquisition.encrypted_content

        # Ensure content is registered with LCP Server
        if not ContentEncryptionService.is_ready_for_licensing(acquisition):
            raise ValueError(f"Content not ready for licensing. Current status: {encrypted_content.status}")

        # Create License record
        license = License.objects.create(
            entry=entry,
            user=user,
            encrypted_content=encrypted_content,
            starts_at=start_date,
            expires_at=end_date,
            passphrase_hint=passphrase_hint,
            state=License.LicenseState.READY,
        )

        try:
            # Generate LCP license via License Server
            lcp_client = LCPServerClient()
            lcp_license = lcp_client.generate_license(
                license,
                user_passphrase=user_passphrase,
                passphrase_hash=passphrase_hash,
                print_limit=print_limit,
                copy_limit=copy_limit,
            )

            # Register with Status Server
            status_client = StatusServerClient()
            status_client.register_license(lcp_license)

            return license

        except Exception as e:
            # If LCP generation/registration fails, delete the license and raise
            license.delete()
            raise ValueError(f"Failed to generate LCP license: {str(e)}")

    @staticmethod
    def fetch_fresh_license(license: License) -> Dict:
        """
        Fetch a fresh license from LCP Server.

        Used by License Gateway endpoint when reading apps request .lcpl file.

        Args:
            license: License to fetch

        Returns:
            Complete LCP license as JSON dict

        Raises:
            ValueError: If license not in valid state
        """
        if not license.lcp_license_id:
            raise ValueError("License does not have an LCP license ID")

        if license.state == License.LicenseState.REVOKED:
            raise ValueError("License has been revoked")

        if license.state == License.LicenseState.CANCELLED:
            raise ValueError("License has been cancelled")

        if license.is_expired:
            raise ValueError("License has expired")

        lcp_client = LCPServerClient()
        return lcp_client.fetch_fresh_license(license)

    @staticmethod
    def renew_license(license: License, new_duration_days: int = 14) -> License:
        """
        Renew a license with extended end date.

        Args:
            license: License to renew
            new_duration_days: Additional days to add from now

        Returns:
            Updated License

        Raises:
            ValueError: If license cannot be renewed
        """
        if license.state not in [
            License.LicenseState.READY,
            License.LicenseState.ACTIVE,
        ]:
            raise ValueError(f"Cannot renew license in state: {license.state}")

        new_end_date = timezone.now() + timedelta(days=new_duration_days)

        # Update via Status Server
        status_client = StatusServerClient()
        status_client.renew_license(license, new_end_date)

        return license

    @staticmethod
    def return_license(license: License) -> License:
        """
        Return a license (end it immediately).

        Args:
            license: License to return

        Returns:
            Updated License

        Raises:
            ValueError: If license cannot be returned
        """
        if license.state not in [
            License.LicenseState.READY,
            License.LicenseState.ACTIVE,
        ]:
            raise ValueError(f"Cannot return license in state: {license.state}")

        # Return via Status Server
        status_client = StatusServerClient()
        status_client.return_license(license)

        return license

    @staticmethod
    def revoke_license(license: License, reason: str = "Revoked by administrator") -> License:
        """
        Revoke a license (permanent).

        Args:
            license: License to revoke
            reason: Reason for revocation

        Returns:
            Updated License
        """
        status_client = StatusServerClient()
        status_client.revoke_license(license, reason)

        return license

    @staticmethod
    def cancel_license(license: License, reason: str = "Cancelled by user") -> License:
        """
        Cancel a license.

        Args:
            license: License to cancel
            reason: Reason for cancellation

        Returns:
            Updated License
        """
        status_client = StatusServerClient()
        status_client.cancel_license(license, reason)

        return license

    @staticmethod
    def get_license_status(license: License) -> Dict:
        """
        Get current license status from Status Server.

        Args:
            license: License to check

        Returns:
            License status info from Status Server
        """
        status_client = StatusServerClient()
        return status_client.get_license_status(license)

    @staticmethod
    def get_registered_devices(license: License) -> list:
        """
        Get devices registered for this license.

        Args:
            license: License to check

        Returns:
            List of registered device info
        """
        status_client = StatusServerClient()
        return status_client.get_registered_devices(license)

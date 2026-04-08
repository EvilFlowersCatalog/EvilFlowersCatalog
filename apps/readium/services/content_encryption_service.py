"""
Content Encryption Service

Handles all encryption-related operations for Readium LCP.
Uses per-publication encryption (Standard LCP approach).
"""

from django.conf import settings
from django.utils import timezone
from typing import Optional
import uuid

from apps.core.models import Acquisition
from apps.readium.models import EncryptedContent
from apps.events.services import get_event_broker


class ContentEncryptionService:
    """
    Service for managing content encryption with LCP.

    Handles:
    - Triggering encryption for publications
    - Tracking encryption status
    - Registering encrypted content with LCP Server
    - Providing URLs for encrypted content
    """

    @staticmethod
    def encrypt_acquisition(acquisition: Acquisition) -> EncryptedContent:
        """
        Trigger encryption for an acquisition.

        This is called when an Entry with readium_enabled=True has a new Acquisition.
        Creates EncryptedContent record and queues encryption worker.

        Args:
            acquisition: The Acquisition to encrypt

        Returns:
            EncryptedContent: The created EncryptedContent record

        Raises:
            ValueError: If acquisition is not eligible for encryption
        """
        # Validate
        if not acquisition.entry.read_config("readium_enabled"):
            raise ValueError("Entry is not readium-enabled")

        if not acquisition.content:
            raise ValueError("Acquisition has no content file")

        # Check if already encrypted
        if hasattr(acquisition, "encrypted_content"):
            return acquisition.encrypted_content

        # Generate unique LCP content ID
        lcp_content_id = str(uuid.uuid4())

        # Determine encrypted file path
        # Format: catalogs/{catalog}/{entry}/encrypted/{lcp_content_id}
        encrypted_path = f"{acquisition.upload_base_path()}/encrypted/{lcp_content_id}"

        # Create EncryptedContent record
        encrypted_content = EncryptedContent.objects.create(
            acquisition=acquisition,
            status=EncryptedContent.EncryptionStatus.PENDING,
            lcp_content_id=lcp_content_id,
            encrypted_path=encrypted_path,
        )

        # Queue encryption worker
        ContentEncryptionService._queue_encryption_task(encrypted_content)

        return encrypted_content

    @staticmethod
    def _queue_encryption_task(encrypted_content: EncryptedContent):
        """Queue the lcpencrypt worker task."""
        acquisition = encrypted_content.acquisition

        # Update status
        encrypted_content.status = EncryptedContent.EncryptionStatus.ENCRYPTING
        encrypted_content.save()

        # Queue worker
        # storage = catalog-relative dir for encrypted output (worker prepends STORAGE_PATH)
        # filename = just the lcp_content_id (no extension) → clean URL
        # url = public base URL → LCP server registers {url}/{filename} as content location
        event_broker = get_event_broker()
        event_broker.execute(
            "evilflowers_lcpencrypt_worker.lcpencrypt",
            {
                "kwargs": {
                    "input_file": acquisition.content.name,
                    "contentid": encrypted_content.lcp_content_id,
                    "storage": f"{acquisition.upload_base_path()}/encrypted",
                    "filename": encrypted_content.lcp_content_id,
                    "lcpsv": getattr(settings, "EVILFLOWERS_READIUM_LCPSV_URL", None),
                    "notify": getattr(settings, "EVILFLOWERS_READIUM_LCPENCRYPT_NOTIFY_URL", None),
                    "url": f"{settings.EVILFLOWERS_READIUM_BASE_URL}/readium/v1/content",
                },
                "queue": "evilflowers_lcpencrypt_worker",
            },
        )

    @staticmethod
    def mark_encryption_completed(lcp_content_id: str, encrypted_url: Optional[str] = None) -> EncryptedContent:
        """
        Mark encryption as completed. Called by webhook after lcpencrypt finishes.

        Args:
            lcp_content_id: The LCP content ID
            encrypted_url: Optional public URL for encrypted content

        Returns:
            EncryptedContent: Updated record
        """
        encrypted_content = EncryptedContent.objects.get(lcp_content_id=lcp_content_id)
        encrypted_content.status = EncryptedContent.EncryptionStatus.COMPLETED
        encrypted_content.encrypted_at = timezone.now()

        if encrypted_url:
            encrypted_content.encrypted_url = encrypted_url
        else:
            # Generate default URL
            encrypted_content.encrypted_url = ContentEncryptionService.get_encrypted_content_url(encrypted_content)

        encrypted_content.save()
        return encrypted_content

    @staticmethod
    def mark_encryption_failed(lcp_content_id: str, error_message: str):
        """Mark encryption as failed."""
        encrypted_content = EncryptedContent.objects.get(lcp_content_id=lcp_content_id)
        encrypted_content.status = EncryptedContent.EncryptionStatus.FAILED
        encrypted_content.error_message = error_message
        encrypted_content.save()

    @staticmethod
    def mark_registered_with_lcp_server(encrypted_content: EncryptedContent):
        """Mark content as registered with LCP Server."""
        encrypted_content.status = EncryptedContent.EncryptionStatus.REGISTERED
        encrypted_content.registered_at = timezone.now()
        encrypted_content.save()

    @staticmethod
    def get_encrypted_content_url(encrypted_content: EncryptedContent) -> str:
        """
        Generate public URL for accessing encrypted content.

        This URL is included in LCP licenses so reading apps can download encrypted files.
        """
        return f"{settings.EVILFLOWERS_READIUM_BASE_URL}/readium/v1/content/{encrypted_content.lcp_content_id}"

    @staticmethod
    def get_by_lcp_content_id(lcp_content_id: str) -> Optional[EncryptedContent]:
        """Get EncryptedContent by LCP content ID."""
        try:
            return EncryptedContent.objects.get(lcp_content_id=lcp_content_id)
        except EncryptedContent.DoesNotExist:
            return None

    @staticmethod
    def is_ready_for_licensing(acquisition: Acquisition) -> bool:
        """
        Check if an acquisition is ready for license generation.

        Returns True if encrypted and registered with LCP Server.
        """
        if not hasattr(acquisition, "encrypted_content"):
            return False

        return acquisition.encrypted_content.status == EncryptedContent.EncryptionStatus.REGISTERED

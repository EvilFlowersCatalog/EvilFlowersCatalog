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
        # lcpencrypt renames output with LCP-specific extensions:
        # .pdf → .lcpdf, .epub → .epub
        # (IP-008 Phase 2 B3: audiobook branch dropped — AcquisitionMIME
        # has no AUDIOBOOK enum value, so the entry was unreachable.
        # Reintroduce when a real audiobook proposal arrives.)
        lcp_ext_map = {
            "application/pdf": ".lcpdf",
            "application/epub+zip": ".epub",
        }
        lcp_ext = lcp_ext_map.get(acquisition.mime, ".lcpdf")
        encrypted_path = f"{acquisition.upload_base_path()}/encrypted/{lcp_content_id}{lcp_ext}"

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
        """Queue the lcpencrypt worker task.

        IP-008 Phase 1 A5: do NOT flip status to REGISTERED here. The
        previous "optimistic mark" let `is_ready_for_licensing` return
        True before the encryption job actually wrote the encrypted file
        and registered it with the LCP server. The webhook
        (`apps/readium/views/hooks.py`) is the authoritative event for
        the flip; we stay on PENDING until then.
        """
        acquisition = encrypted_content.acquisition

        # The encrypted_url is computed deterministically from the
        # lcp_content_id and base URL, so we can populate it now even
        # though the file isn't ready — the URL is only used by the LCP
        # server once it can fetch the encrypted blob.
        encrypted_content.encrypted_url = ContentEncryptionService.get_encrypted_content_url(encrypted_content)
        encrypted_content.save(update_fields=["encrypted_url"])

        # Queue worker
        # storage = catalog-relative dir for encrypted output (worker prepends STORAGE_PATH)
        # filename = just the lcp_content_id (no extension, lcpencrypt appends .lcpdf/.epub)
        # url = public base URL → LCP server registers {url}/{filename} as content location
        # No -notify: the LCP server registration via -lcpsv is sufficient
        #
        # title/author: publication display metadata. For the LCP-for-PDF
        # profile (`.lcpdf`) lcpencrypt wraps the raw PDF into a Readium
        # package and generates its `manifest.json`; a raw PDF carries no
        # embedded title/author, so without these the manifest falls back to
        # the filename (title) and empty authors — which is why Thorium shows
        # "no title and no authors available" for borrowed PDFs. We forward
        # the Entry's metadata so the worker can inject it into the package
        # manifest. (The worker must consume these keys — see
        # evilflowers-lcpencrypt-worker; unknown keys are ignored by older
        # workers, so sending them is backwards compatible.)
        entry = acquisition.entry
        author_name = entry.first_author_name
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
                    "url": f"{settings.EVILFLOWERS_READIUM_BASE_URL}/readium/v1/content",
                    "title": entry.title,
                    "author": author_name,
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

        Returns True only when:
        - The EncryptedContent row exists,
        - Status is REGISTERED (set by the webhook after lcpencrypt finishes
          and the LCP server confirms registration), AND
        - `encrypted_at is not None` (IP-008 Phase 1 A5 defensive check —
          a misbehaving webhook could in principle PATCH status without
          setting the timestamp; both must be true).
        """
        if not hasattr(acquisition, "encrypted_content"):
            return False

        ec = acquisition.encrypted_content
        return ec.status == EncryptedContent.EncryptionStatus.REGISTERED and ec.encrypted_at is not None

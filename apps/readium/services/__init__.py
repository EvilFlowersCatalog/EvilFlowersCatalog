"""
Readium LCP Service Layer

This module provides clean service classes for Readium LCP integration:

- ContentEncryptionService: Manages content encryption lifecycle
- LCPServerClient: Communicates with LCP License Server
- StatusServerClient: Communicates with LCP Status Server
- LicenseService: Manages license lifecycle (availability, creation, renewal, etc.)
"""

from .content_encryption_service import ContentEncryptionService
from .lcp_server_client import LCPServerClient
from .lsd_transport import LsdTransport, LsdTransportError
from .status_server_client import StatusServerClient
from .status_server_sync import StatusServerSyncService
from .license_service import LicenseService, PassphraseRequiredError
from .reservation_service import ReservationService

__all__ = [
    "ContentEncryptionService",
    "LCPServerClient",
    "LsdTransport",
    "LsdTransportError",
    "StatusServerClient",
    "StatusServerSyncService",
    "LicenseService",
    "PassphraseRequiredError",
    "ReservationService",
]

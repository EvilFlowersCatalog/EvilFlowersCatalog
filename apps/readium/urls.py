"""
Readium LCP URL Configuration

Endpoints:
- /hooks/encryption - Webhook for lcpencrypt worker notifications
- /content/{id} - Download encrypted publications
- /licenses - License CRUD operations
- /licenses/{id}.lcpl - License Gateway (download .lcpl files)
- /licenses/{id}/status - LSD proxy: License Status Document
- /licenses/{id}/register - LSD proxy: Device registration
- /licenses/{id}/return - LSD proxy: Loan return
- /licenses/{id}/renew - LSD proxy: Loan renewal
- /entries/{id}/availability - Availability calendar
- /entries/{id}/encryption - Encryption status and manual trigger
- /publications/{id}/manifest.json - RWPM manifest
- /hint - Passphrase hint page
"""

from django.urls import path, re_path

from apps.readium.views.content import EncryptedContentDownloadView
from apps.readium.views.hooks import EncryptionWebhook
from apps.readium.views.licenses import LicenseManagement, LicenseDetail
from apps.readium.views.availability import EntryAvailabilityView
from apps.readium.views.download import LicenseDownloadView
from apps.readium.views.encryption import EntryEncryptionView
from apps.readium.views.hint import HintPageView
from apps.readium.views.manifest import PublicationManifestView
from apps.readium.views.status_proxy import (
    DeviceRegistrationProxyView,
    RenewProxyView,
    ReturnProxyView,
    StatusDocumentView,
)

urlpatterns = [
    # Webhooks
    path("hooks/encryption", EncryptionWebhook.as_view(), name="encryption-webhook"),
    # Encrypted content download (reading apps fetch encrypted publications here)
    # Matches both /content/{uuid} and /content/{uuid}.lcpdf (lcpencrypt appends LCP extension)
    re_path(
        r"content/(?P<lcp_content_id>[a-f0-9-]+)",
        EncryptedContentDownloadView.as_view(),
        name="encrypted-content-download",
    ),
    # License Management
    path("licenses", LicenseManagement.as_view(), name="license-management"),
    path("licenses/<uuid:license_id>", LicenseDetail.as_view(), name="license-detail"),
    # License Gateway (reading apps download .lcpl files here)
    path("licenses/<uuid:license_id>.lcpl", LicenseDownloadView.as_view(), name="license-gateway"),
    # LSD Proxy (Status Server -- never exposed directly)
    path("licenses/<uuid:license_id>/status", StatusDocumentView.as_view(), name="lsd-status"),
    path("licenses/<uuid:license_id>/register", DeviceRegistrationProxyView.as_view(), name="lsd-register"),
    path("licenses/<uuid:license_id>/return", ReturnProxyView.as_view(), name="lsd-return"),
    path("licenses/<uuid:license_id>/renew", RenewProxyView.as_view(), name="lsd-renew"),
    # Availability
    path("entries/<uuid:entry_id>/availability", EntryAvailabilityView.as_view(), name="entry-availability"),
    # Encryption Management
    path("entries/<uuid:entry_id>/encryption", EntryEncryptionView.as_view(), name="entry-encryption"),
    # Publication Manifest (RWPM)
    path(
        "publications/<uuid:entry_id>/manifest.json",
        PublicationManifestView.as_view(),
        name="publication-manifest",
    ),
    # Passphrase Hint Page
    path("hint", HintPageView.as_view(), name="hint"),
]
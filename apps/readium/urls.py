"""
Readium LCP URL Configuration

Endpoints:
- /hooks/encryption - Webhook for lcpencrypt worker notifications
- /licenses - License CRUD operations
- /licenses/{id}.lcpl - License Gateway (download .lcpl files)
- /entries/{id}/availability - Availability calendar
- /entries/{id}/encryption - Encryption status and manual trigger
"""

from django.urls import path, re_path

from apps.readium.views.content import EncryptedContentDownloadView
from apps.readium.views.hooks import EncryptionWebhook
from apps.readium.views.licenses import LicenseManagement, LicenseDetail
from apps.readium.views.availability import EntryAvailabilityView
from apps.readium.views.download import LicenseDownloadView
from apps.readium.views.encryption import EntryEncryptionView

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
    # Availability
    path("entries/<uuid:entry_id>/availability", EntryAvailabilityView.as_view(), name="entry-availability"),
    # Encryption Management
    path("entries/<uuid:entry_id>/encryption", EntryEncryptionView.as_view(), name="entry-encryption"),
]

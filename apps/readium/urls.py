"""
Readium LCP URL Configuration

Endpoints:
- /hooks/encryption - Webhook for lcpencrypt worker notifications
- /licenses - License CRUD operations
- /licenses/{id}.lcpl - License Gateway (download .lcpl files)
- /entries/{id}/availability - Availability calendar
"""

from django.urls import path

from apps.readium.views.hooks import EncryptionWebhook
from apps.readium.views.licenses import LicenseManagement, LicenseDetail
from apps.readium.views.availability import EntryAvailabilityView
from apps.readium.views.download import LicenseDownloadView

urlpatterns = [
    # Webhooks
    path("hooks/encryption", EncryptionWebhook.as_view(), name="encryption-webhook"),

    # License Management
    path("licenses", LicenseManagement.as_view(), name="license-management"),
    path("licenses/<uuid:license_id>", LicenseDetail.as_view(), name="license-detail"),

    # License Gateway (reading apps download .lcpl files here)
    path("licenses/<uuid:license_id>.lcpl", LicenseDownloadView.as_view(), name="license-gateway"),

    # Availability
    path("entries/<uuid:entry_id>/availability", EntryAvailabilityView.as_view(), name="entry-availability"),
]

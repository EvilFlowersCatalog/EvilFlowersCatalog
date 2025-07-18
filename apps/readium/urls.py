from django.urls import path

from apps.readium.views.hooks import ReadiumHook
from apps.readium.views.licenses import LicenseManagement, LicenseDetail
from apps.readium.views.availability import EntryAvailabilityView, EntryLicensesView
from apps.readium.views.download import LicenseDownloadView

urlpatterns = [
    path("hooks", ReadiumHook.as_view(), name="redium-hooks"),
    path("licenses", LicenseManagement.as_view(), name="license-management"),
    path("licenses/<uuid:license_id>", LicenseDetail.as_view(), name="license-detail"),
    path("licenses/<uuid:license_id>.lcpl", LicenseDownloadView.as_view(), name="license-file"),
    path("entries/<uuid:entry_id>/availability", EntryAvailabilityView.as_view(), name="entry-availability"),
    path("entries/<uuid:entry_id>/licenses", EntryLicensesView.as_view(), name="entry-licenses"),
]

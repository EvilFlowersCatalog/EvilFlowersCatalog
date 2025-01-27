from django.urls import path

from apps.readium.views.hooks import ReadiumHook
from apps.readium.views.licenses import LicenseManagement, LicenseDetail

urlpatterns = [
    path("hooks", ReadiumHook.as_view(), name="redium-hooks"),
    path("licenses", LicenseManagement.as_view(), name="license-management"),
    path("licenses/<uuid:license_id>", LicenseDetail.as_view(), name="license-detail"),
]

from django.urls import path

from apps.files.views import AcquisitionDownload

urlpatterns = [
    # Acquisitions
    path("<uuid:acquisition_id>", AcquisitionDownload.as_view(), name='acquisition_download')
]

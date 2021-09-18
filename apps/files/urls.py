from django.urls import path

from apps.files.views import AcquisitionDownload, EntryImageDownload

urlpatterns = [
    # Acquisitions
    path("acquisitions/<uuid:acquisition_id>", AcquisitionDownload.as_view(), name='acquisition_download'),
    path("covers/<uuid:entry_id>", EntryImageDownload.as_view(), name='cover_download')
]

from django.urls import path

from apps.files.views import AcquisitionDownload, EntryImageDownload, UserAcquisitionDownload

urlpatterns = [
    # Acquisitions
    path("acquisitions/<uuid:acquisition_id>", AcquisitionDownload.as_view(), name="acquisition-download"),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>",
        UserAcquisitionDownload.as_view(),
        name="user-acquisition-download",
    ),
    path("covers/<uuid:entry_id>", EntryImageDownload.as_view(), name="cover-download"),
]

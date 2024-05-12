from django.urls import path

from apps.files.views import (
    AcquisitionDownload,
    EntryImageDownload,
    UserAcquisitionDownload,
    EntryThumbnailDownload,
)

urlpatterns = [
    # Acquisitions
    path(
        "acquisitions/<uuid:acquisition_id>",
        AcquisitionDownload.as_view(),
        name="acquisition-download",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>",
        UserAcquisitionDownload.as_view(),
        name="user-acquisition-download",
    ),
    path("covers/<uuid:entry_id>", EntryImageDownload.as_view(), name="cover-download"),
    path("thumbnails/<uuid:entry_id>", EntryThumbnailDownload.as_view(), name="thumbnail-download"),
]

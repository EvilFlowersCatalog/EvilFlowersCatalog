from django.urls import path

from apps.files.views import (
    AcquisitionDownload,
    EntryImageDownload,
    UserAcquisitionDownload,
    EntryThumbnailDownload,
)
from apps.extraction.discovery import EntryRegionImage
from apps.extraction.views import UserAcquisitionRegionImage

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
    path(
        "user-acquisitions/<uuid:user_acquisition_id>/regions/<uuid:region_id>.webp",
        UserAcquisitionRegionImage.as_view(),
        name="user-acquisition-region-image",
    ),
    path(
        "entries/<uuid:entry_id>/regions/<uuid:region_id>.webp",
        EntryRegionImage.as_view(),
        name="entry-region-image",
    ),
    path("covers/<uuid:entry_id>", EntryImageDownload.as_view(), name="cover-download"),
    path("thumbnails/<uuid:entry_id>", EntryThumbnailDownload.as_view(), name="thumbnail-download"),
]

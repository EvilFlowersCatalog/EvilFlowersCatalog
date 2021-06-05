from django.urls import path

from apps.api.views import catalogs, feeds, entries, acquisitions

urlpatterns = [
    # Catalogs
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),

    # Feeds
    path("feeds", feeds.FeedManagement.as_view()),
    path("feeds/<uuid:feed_id>", feeds.FeedDetail.as_view()),

    # Entries
    path("entries", entries.EntryPaginator.as_view()),
    path("catalogs/<uuid:catalog_id>/entries", entries.EntryManagement.as_view()),
    path("catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>", entries.EntryDetail.as_view()),

    # Acquisitions
    path("acquisitions/<uuid:acquisition_id>/download", acquisitions.AcquisitionDownload.as_view(), name='acquisition_download')
]

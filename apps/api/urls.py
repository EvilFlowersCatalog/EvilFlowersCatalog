from django.urls import path

from apps.api.views import catalogs, feeds

urlpatterns = [
    # Catalogs
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),

    # Feeds
    path("feeds", feeds.FeedManagement.as_view()),
    path("feeds/<uuid:feed_id>", feeds.FeedDetail.as_view()),
]

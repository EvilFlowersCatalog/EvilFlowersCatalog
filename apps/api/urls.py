from django.urls import path

from apps.api.views import catalogs, feeds, entries, api_keys, users, acquisitions, authors

urlpatterns = [
    # API keys
    path("api_keys", api_keys.ApiKeyManagement.as_view()),
    path("api_keys/<uuid:api_key_id>", api_keys.ApiKeyDetail.as_view()),

    # Users
    path('users', users.UserManagement.as_view()),
    path('users/<uuid:user_id>', users.UserDetail.as_view()),

    # Catalogs
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),

    # Feeds
    path("feeds", feeds.FeedManagement.as_view()),
    path("feeds/<uuid:feed_id>", feeds.FeedDetail.as_view()),

    # Authors
    path("authors", authors.AuthorManagement.as_view()),
    path("authors/<uuid:author_id>", authors.AuthorDetail.as_view()),

    # Entries
    path("entries", entries.EntryPaginator.as_view()),
    path("catalogs/<uuid:catalog_id>/entries", entries.EntryManagement.as_view()),
    path("catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>", entries.EntryDetail.as_view()),

    # Acquisitions
    path("acquisitions/<uuid:acquisition_id>", acquisitions.AcquisitionDetail.as_view()),
]

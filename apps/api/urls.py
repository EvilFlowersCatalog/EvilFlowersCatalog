from django.urls import path

from apps.api.views import (
    catalogs,
    feeds,
    entries,
    api_keys,
    users,
    acquisitions,
    authors,
    status,
    tokens,
    user_acquisitions,
    annotations,
    shelf_records,
    categories,
    annotation_items,
    languages,
)

urlpatterns = [
    # API keys
    path("api_keys", api_keys.ApiKeyManagement.as_view()),
    path("api_keys/<uuid:api_key_id>", api_keys.ApiKeyDetail.as_view()),
    # Users
    path("users", users.UserManagement.as_view()),
    path("users/me", users.UserMe.as_view()),
    path("users/<uuid:user_id>", users.UserDetail.as_view()),
    # Catalogs
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),
    # Feeds
    path("feeds", feeds.FeedManagement.as_view()),
    path("feeds/<uuid:feed_id>", feeds.FeedDetail.as_view()),
    # Authors
    path("authors", authors.AuthorManagement.as_view()),
    path("authors/<uuid:author_id>", authors.AuthorDetail.as_view()),
    # Categories
    path(
        "categories",
        categories.CategoryManagement.as_view(),
        name="category-management",
    ),
    path(
        "categories/<uuid:category_id>",
        categories.CategoryDetail.as_view(),
        name="category-detail",
    ),
    # Entries
    path("entries", entries.EntryPaginator.as_view()),
    path("entry-introspection", entries.EntryIntrospection.as_view()),
    path("catalogs/<uuid:catalog_id>/entries", entries.EntryManagement.as_view()),
    path(
        "catalogs/<uuid:catalog_id>/entries/<uuid:entry_id>",
        entries.EntryDetail.as_view(),
    ),
    # Acquisitions
    path(
        "acquisitions",
        acquisitions.AcquisitionManagement.as_view(),
        name="acquisition-management",
    ),
    path(
        "acquisitions/<uuid:acquisition_id>",
        acquisitions.AcquisitionDetail.as_view(),
        name="acquisition-detail",
    ),
    # Shelves
    path(
        "shelf-records",
        shelf_records.ShelfRecordManagement.as_view(),
        name="shelf-record-management",
    ),
    path(
        "shelf-records/<uuid:shelf_record_id>",
        shelf_records.ShelfRecordDetail.as_view(),
        name="shelf-record-detail",
    ),
    # User acquisitions
    path(
        "user-acquisitions",
        user_acquisitions.UserAcquisitionManagement.as_view(),
        name="user-acquisition-management",
    ),
    path(
        "user-acquisitions/<uuid:user_acquisition_id>",
        user_acquisitions.UserAcquisitionDetail.as_view(),
        name="user-acquisition-detail",
    ),
    # Annotations
    path(
        "annotations",
        annotations.AnnotationManagement.as_view(),
        name="annotation-management",
    ),
    path(
        "annotations/<uuid:annotation_id>",
        annotations.AnnotationDetail.as_view(),
        name="annotation-detail",
    ),
    path(
        "annotation-items",
        annotation_items.AnnotationItemManagement.as_view(),
        name="annotation-item-management",
    ),
    path(
        "annotation-items/<uuid:annotation_item_id>",
        annotation_items.AnnotationItemDetail.as_view(),
        name="annotation-item-detail",
    ),
    # Status
    path("status", status.StatusManagement.as_view(), name="status"),
    # Tokens
    path("token/refresh", tokens.RefreshTokenManagement.as_view(), name="refresh"),
    path("token", tokens.AccessTokenManagement.as_view(), name="login"),
    path("languages", languages.LanguageManagement.as_view(), name="languages"),
]

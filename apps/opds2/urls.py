from django.urls import path

from apps.opds2.views import (
    AuthenticationDocumentView,
    BorrowView,
    CatalogView,
    FeedView,
    NavigationView,
    NewView,
    PopularView,
    PublicationDetailView,
    PublicationFeedView,
    PublicationManifestView,
    RenewView,
    ReturnView,
    SearchView,
    ShelfView,
)

urlpatterns = [
    # Authentication (no catalog prefix)
    path("auth", AuthenticationDocumentView.as_view(), name="auth"),
    # Catalog root
    path("<str:catalog_name>/", CatalogView.as_view(), name="catalog"),
    # Publications
    path("<str:catalog_name>/publications", PublicationFeedView.as_view(), name="publications"),
    path(
        "<str:catalog_name>/publications/<uuid:entry_id>",
        PublicationDetailView.as_view(),
        name="publication-detail",
    ),
    path(
        "<str:catalog_name>/publications/<uuid:entry_id>/manifest.json",
        PublicationManifestView.as_view(),
        name="publication-manifest",
    ),
    # Borrowing
    path(
        "<str:catalog_name>/publications/<uuid:entry_id>/borrow",
        BorrowView.as_view(),
        name="borrow",
    ),
    path(
        "<str:catalog_name>/publications/<uuid:entry_id>/return",
        ReturnView.as_view(),
        name="return",
    ),
    path(
        "<str:catalog_name>/publications/<uuid:entry_id>/renew",
        RenewView.as_view(),
        name="renew",
    ),
    # Navigation
    path("<str:catalog_name>/navigation", NavigationView.as_view(), name="navigation"),
    path("<str:catalog_name>/feed/<str:feed_name>", FeedView.as_view(), name="feed"),
    path("<str:catalog_name>/new", NewView.as_view(), name="new"),
    path("<str:catalog_name>/popular", PopularView.as_view(), name="popular"),
    # Search
    path("<str:catalog_name>/search", SearchView.as_view(), name="search"),
    # Shelf
    path("<str:catalog_name>/shelf", ShelfView.as_view(), name="shelf"),
]

from django.urls import path

from apps.opds.views.catalog import RootView
from apps.opds.views.entry import EntryView
from apps.opds.views.feed import PopularFeedView, LatestFeedView, ShelfFeedView, CompleteFeedView, FeedView
from apps.opds.views.search import SearchView

urlpatterns = [
    # Feeds
    path("<str:catalog_name>", RootView.as_view(), name='root'),
    path("<str:catalog_name>/popular", PopularFeedView.as_view(), name='popular'),
    path("<str:catalog_name>/new", LatestFeedView.as_view(), name='new'),
    path("<str:catalog_name>/shelf", ShelfFeedView.as_view(), name='shelf'),
    path("<str:catalog_name>/complete", CompleteFeedView.as_view(), name='complete'),
    path("<str:catalog_name>/feed/<str:feed_name>", FeedView.as_view(), name='feed'),

    # Search
    path("<str:catalog_name>/search", SearchView.as_view(), name='catalog_search'),
    path("<str:catalog_name>/search/<str:feed_name>", SearchView.as_view(), name='feed_search'),

    # Entries
    path("<str:catalog_name>/entries/<uuid:entry_id>", EntryView.as_view(), name='complete_entry'),
]

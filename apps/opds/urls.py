from django.urls import path

from apps.opds.views.catalog import RootView, FeedView, PopularFeedView, LatestFeedView, ShelfFeedView

urlpatterns = [
    path("<str:catalog_name>/root.xml", RootView.as_view(), name='root'),
    path(r"<str:catalog_name>/popular.xml", PopularFeedView.as_view(), name='popular'),
    path(r"<str:catalog_name>/new.xml", LatestFeedView.as_view(), name='new'),
    path("<str:catalog_name>/shelf.xml", ShelfFeedView.as_view(), name='shelf'),
    path("<str:catalog_name>/feed/<str:feed_name>.xml", FeedView.as_view(), name='feed'),
]

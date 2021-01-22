from django.urls import path

from apps.opds.views.catalog import catalog_detail, feed_detail, relation_popular, relation_new, relation_shelf

urlpatterns = [
    path("<str:catalog_name>/root.xml", catalog_detail, name='catalog-detail'),
    path(r"<str:catalog_name>/popular.xml", relation_popular, name='relation-popular'),
    path(r"<str:catalog_name>/new.xml", relation_new, name='relation-new'),
    path("<str:catalog_name>/shelf.xml", relation_shelf, name='relation-shelf'),
    path("<str:catalog_name>/feeds/<str:feed_name>.xml", feed_detail, name='feed-detail'),
]

from django.urls import path

from apps.opds.views.catalog import root, subsection, feed_popular, feed_new, feed_shelf

urlpatterns = [
    path("<str:catalog_name>/root.xml", root, name='root'),
    path(r"<str:catalog_name>/popular.xml", feed_popular, name='popular'),
    path(r"<str:catalog_name>/new.xml", feed_new, name='new'),
    path("<str:catalog_name>/shelf.xml", feed_shelf, name='shelf'),
    path("<str:catalog_name>/subsections/<str:feed_name>.xml", subsection, name='subsection'),
]

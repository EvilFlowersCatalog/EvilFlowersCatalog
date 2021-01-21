from django.urls import path

from apps.opds.views.catalog import catalog_detail, feed_detail

urlpatterns = [
    path("<str:url_name>/root.xml", catalog_detail),
    path("<str:catalog_name>/<str:feed_name>.xml", feed_detail),
]

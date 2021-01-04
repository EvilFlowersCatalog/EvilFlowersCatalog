from django.urls import path

from apps.opds.views.catalog import catalog_detail

urlpatterns = [
    path("<str:url_name>/root.xml", catalog_detail)
]

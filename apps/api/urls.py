from django.urls import path

from apps.api.views import catalogs

urlpatterns = [
    path("catalogs", catalogs.CatalogManagement.as_view()),
    path("catalogs/<uuid:catalog_id>", catalogs.CatalogDetail.as_view()),
]

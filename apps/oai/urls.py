"""IP-033 routes, mounted at the site root: `/oai`, `/resolve/isbn/<isbn>`, `/availability`, `/api/v1/oai/status`."""

from django.urls import path

from apps.oai.views import AvailabilityView, IsbnResolveView, OaiPmhView, OaiStatusView

app_name = "oai"

urlpatterns = [
    path("oai", OaiPmhView.as_view(), name="oai-pmh"),
    path("resolve/isbn/<str:isbn>", IsbnResolveView.as_view(), name="resolve-isbn"),
    path("availability", AvailabilityView.as_view(), name="availability"),
    path("api/v1/oai/status", OaiStatusView.as_view(), name="status"),
]

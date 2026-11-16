"""IP-034 routes mounted at the site root: the generic resolver `/resolve/<source slug>?id=<record id>`."""

from django.urls import path

from apps.harvest.resolve import ResolveView

app_name = "harvest-public"

urlpatterns = [
    path("resolve/<slug:source_slug>", ResolveView.as_view(), name="resolve"),
]

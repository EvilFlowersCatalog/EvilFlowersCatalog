"""IP-034 admin API, mounted under `/api/v1/`: `harvest/sources…`, `external-systems`."""

from django.urls import path

from apps.harvest import views

app_name = "harvest"

urlpatterns = [
    path("harvest/adapters", views.AdapterTypeManagement.as_view(), name="adapters"),
    path("harvest/sources", views.HarvestSourceManagement.as_view(), name="source-management"),
    path("harvest/sources/<uuid:source_id>", views.HarvestSourceDetail.as_view(), name="source-detail"),
    path("harvest/sources/<uuid:source_id>/test", views.HarvestSourceTest.as_view(), name="source-test"),
    path("harvest/sources/<uuid:source_id>/upload", views.HarvestSourceUpload.as_view(), name="source-upload"),
    path(
        "harvest/sources/<uuid:source_id>/records", views.HarvestRecordManagement.as_view(), name="record-management"
    ),
    path("external-systems", views.ExternalSystemManagement.as_view(), name="external-systems"),
    # The proposal's name for `harvest/adapters`.
    path("harvest/connectors", views.AdapterTypeManagement.as_view(), name="connectors"),
    path("harvest/conflicts", views.ConflictManagement.as_view(), name="conflict-management"),
    path(
        "harvest/conflicts/<uuid:conflict_id>/acknowledge",
        views.ConflictAcknowledge.as_view(),
        name="conflict-acknowledge",
    ),
    path("entries/<uuid:entry_id>/sync", views.EntrySyncState.as_view(), name="entry-sync"),
]

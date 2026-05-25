from django.urls import path

from apps.dataverse.views import PrepublishView

app_name = "dataverse"

urlpatterns = [
    path("dataverse-prepublish", PrepublishView.as_view(), name="prepublish"),
]

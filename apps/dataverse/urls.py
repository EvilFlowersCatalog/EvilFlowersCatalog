"""Dataverse routes, mounted at the site root (full paths below).

`/api/v1/dataverse-prepublish` itself stays registered in `apps.api.urls` (the path Dataverse workflows call).
IP-034 adds the pre-publish validation hook; the "Open in Elvira" link is the generic `/resolve/<source slug>`
of `apps.harvest`.
"""

from django.urls import path

from apps.dataverse.views import ValidateView

app_name = "dataverse"

urlpatterns = [
    path("api/v1/dataverse-validate", ValidateView.as_view(), name="validate"),
]

from django.urls import path

from apps.openapi.views import OpenApiManagement

urlpatterns = [
    path("", OpenApiManagement.as_view()),
]

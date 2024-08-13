"""updater_api URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.views.static import serve

urlpatterns = [
    path("api/v1/", include(("apps.api.urls", "api"), namespace="api")),
    path("opds/v1.2/", include(("apps.opds.urls", "opds"), namespace="opds")),
    path("opds/v2/", include(("apps.opds2.urls", "opds2"), namespace="opds2")),
    path("data/v1/", include(("apps.files.urls", "files"), namespace="files")),
    path("docs/", include(("apps.openapi.urls", "openapi"), namespace="openapi")),
    path("admin/", admin.site.urls),
    path('django-rq/', include('django_rq.urls'))
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, view=serve)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT, view=serve)

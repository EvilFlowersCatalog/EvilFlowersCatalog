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
    path("readium/v1/", include(("apps.readium.urls", "readium"), namespace="readium")),
    path("data/v1/", include(("apps.files.urls", "files"), namespace="files")),
    path("docs/", include(("apps.openapi.urls", "openapi"), namespace="openapi")),
    path("admin/", admin.site.urls),
]

# IP-014: the MCP server is opt-out. When disabled the route is not mounted at
# all, so a probe gets a plain 404 rather than a disabled endpoint to poke at.
if settings.EVILFLOWERS_MCP_ENABLED:
    from apps.mcp.metadata import ProtectedResourceMetadata

    urlpatterns.append(path("mcp/v1", include(("apps.mcp.urls", "mcp"), namespace="mcp")))
    # RFC 9728 discovery. Both spellings are served: the path-inserted form the
    # MCP authorization spec derives from the resource URL, and the bare
    # well-known that clients probe when they do not implement path insertion.
    urlpatterns.append(
        path(
            ".well-known/oauth-protected-resource/mcp/v1",
            ProtectedResourceMetadata.as_view(),
            name="mcp-protected-resource-metadata",
        )
    )
    urlpatterns.append(
        path(".well-known/oauth-protected-resource", ProtectedResourceMetadata.as_view()),
    )

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, view=serve)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT, view=serve)

"""IP-035: URLs of the console pages, mounted inside the admin site (`ElviraAdminSite.get_urls`).

Every view goes through `site.admin_view`: the admin's session login, the staff check (superusers only,
`User.is_staff`), `csrf_protect` on writes and `never_cache`. A feature package contributes its routes
with a `urlpatterns(site)` function; names are prefixed `console_` and reversed as `admin:console_…`.
"""

from django.urls import path


def console_urlpatterns(site):
    from apps.console.views import graph, grid, jobs, modules, vectors

    return [
        path("console/module/<slug:key>/", site.admin_view(site.module_index), name="console_module"),
        *modules.urlpatterns(site),
        *grid.urlpatterns(site),
        *graph.urlpatterns(site),
        *vectors.urlpatterns(site),
        *jobs.urlpatterns(site),
    ]

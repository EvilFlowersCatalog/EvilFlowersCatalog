import os

from django.http import FileResponse, Http404
from django.views import View

from apps.notifications.branding import ASSET_TYPES, asset_path


class NotificationAsset(View):
    """Public, cacheable images for notification e-mails (IP-025).

    Mail clients fetch the logo without credentials, so this is deliberately
    unauthenticated — and deliberately narrow: only files directly inside
    `<EVILFLOWERS_NOTIFICATION_OVERRIDES_DIR>/assets/` with an image extension
    a mail client renders. The directory is a read-only volume owned by IaC.
    """

    http_method_names = ["get", "head"]

    def get(self, request, name: str):
        path = asset_path(name)
        if path is None:
            raise Http404()
        response = FileResponse(open(path, "rb"), content_type=ASSET_TYPES[os.path.splitext(name)[1].lower()])
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Content-Type-Options"] = "nosniff"
        return response

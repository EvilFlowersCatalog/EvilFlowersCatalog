"""
Passphrase Hint Page

Required by the LCP spec. Reading apps show this page to help users
remember their passphrase. The `hint` link in the .lcpl file points here.
"""

from http import HTTPStatus
from uuid import UUID

from django.http import HttpResponse
from django.views import View

from apps.readium.models import License


class HintPageView(View):
    def get(self, request):
        license_id = request.GET.get("license_id")

        hint = "Your library password"
        if license_id:
            try:
                license_obj = License.objects.get(pk=license_id)
                if license_obj.passphrase_hint:
                    hint = license_obj.passphrase_hint
            except (License.DoesNotExist, ValueError):
                pass

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Passphrase Hint</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               display: flex; justify-content: center; align-items: center; min-height: 100vh;
               margin: 0; background: #f5f5f5; }}
        .card {{ background: white; padding: 2rem; border-radius: 8px;
                 box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; text-align: center; }}
        h1 {{ font-size: 1.2rem; color: #333; margin-bottom: 1rem; }}
        .hint {{ font-size: 1.1rem; color: #666; padding: 1rem; background: #f0f0f0;
                 border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Passphrase Hint</h1>
        <div class="hint">{hint}</div>
    </div>
</body>
</html>"""
        return HttpResponse(html, content_type="text/html")

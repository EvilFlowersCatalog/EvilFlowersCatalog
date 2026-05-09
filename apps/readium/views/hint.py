"""
Passphrase Hint Page

Required by the LCP spec. Reading apps show this page to help users
remember their passphrase. The `hint` link in the .lcpl file points here.
"""

from django.http import HttpResponse
from django.template.loader import render_to_string
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

        html = render_to_string("readium/hint.html", {"hint": hint})
        return HttpResponse(html, content_type="text/html")

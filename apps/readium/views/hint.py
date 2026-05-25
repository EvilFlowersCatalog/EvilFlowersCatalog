"""
Passphrase Hint Page

Required by the LCP spec. Reading apps show this page to help users
remember their passphrase. The `hint` link in the .lcpl file points here.
"""

from http import HTTPStatus

from django.core.cache import cache
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views import View

from apps.readium.models import License

# IP-008 Phase 2 B4: throttle to slow enumeration. The hint page returns
# the same response shape for "no hint set" and "license not found", but
# without a rate limit a scripted attacker could still enumerate license
# UUIDs. 1 request per second per IP is plenty for legit reader-app use.
_HINT_RATELIMIT_KEY = "readium:hint:rl:{ip}"
_HINT_RATELIMIT_SECONDS = 1


class HintPageView(View):
    def get(self, request):
        client_ip = self._client_ip(request)
        cache_key = _HINT_RATELIMIT_KEY.format(ip=client_ip)
        # `cache.add` is atomic: returns False if the key already exists.
        if not cache.add(cache_key, 1, timeout=_HINT_RATELIMIT_SECONDS):
            return HttpResponse(
                status=HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(_HINT_RATELIMIT_SECONDS)},
            )

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

    @staticmethod
    def _client_ip(request) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")

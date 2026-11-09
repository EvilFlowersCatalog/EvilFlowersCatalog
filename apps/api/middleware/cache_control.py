"""Default HTTP caching policy for every catalog response (IP-027, #123).

Most views never said how their response may be cached. `ConditionalGetMiddleware`
still gave them an ETag, so browsers and the edge nginx had to guess, and the
guess differs per cache: nginx on prod stored header-less `/data/v1/` responses
for four weeks under a key that ignores `Authorization`.

The default fails closed. A response that sets no `Cache-Control` of its own gets:

- `no-store` for unsafe methods and for errors (4xx/5xx). A write result or a
  problem document is never worth reusing;
- `EVILFLOWERS_CACHE_CONTROL_DEFAULT` otherwise (`private, no-cache`): only the
  caller's own browser may keep a copy, and it must revalidate it before every
  reuse. The ETag makes that revalidation a cheap 304.

`Vary: Authorization` goes with the default, because almost every body depends on
who asks. A view that knows better — a public cover, an OPDS feed with its own
validator — sets `Cache-Control` itself and is left untouched.

Sits directly below `ConditionalGetMiddleware`, so the headers are in place before
that middleware turns a 200 into a 304 (Django copies them onto the 304).
"""

from django.conf import settings
from django.utils.cache import patch_vary_headers

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CacheControlMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.has_header("Cache-Control"):
            return response

        if request.method not in SAFE_METHODS or response.status_code >= 400:
            response["Cache-Control"] = "no-store"
        else:
            response["Cache-Control"] = settings.EVILFLOWERS_CACHE_CONTROL_DEFAULT
            patch_vary_headers(response, ("Authorization",))
        return response

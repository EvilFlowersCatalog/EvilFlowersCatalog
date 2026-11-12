from urllib.parse import urlsplit

from django.conf import settings

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


class PublicSchemeMiddleware:
    """Treat requests for the public host as https when the public origin is https.

    Behind the k8s edge gateway (dev since 2026-11-07) Envoy rewrites
    `X-Forwarded-Proto` to `http`, so every `request.build_absolute_uri()`
    minted `http://` links (user acquisitions, thumbnails, OPDS, `.lcpl`) that
    browsers block on the https portal as mixed content. TLS is terminated in
    front of us, so a request addressed to the configured https host did arrive
    over https.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        parts = urlsplit(str(getattr(settings, "EVILFLOWERS_BASE_URL", "") or ""))
        host = (parts.hostname or "").lower()
        self.public_host = host if parts.scheme == "https" and host not in _LOOPBACK_HOSTS else ""

    def __call__(self, request):
        if self.public_host:
            host = request.META.get("HTTP_HOST", "").rsplit(":", 1)[0].lower()
            if host == self.public_host:
                request.META["HTTP_X_FORWARDED_PROTO"] = "https"
                request.META["wsgi.url_scheme"] = "https"
        return self.get_response(request)

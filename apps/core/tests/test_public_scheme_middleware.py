from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.api.middleware.public_scheme import PublicSchemeMiddleware


def _absolute_url(request):
    return HttpResponse(request.build_absolute_uri("/x"))


@override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), ALLOWED_HOSTS=["*"])
class PublicSchemeMiddlewareTests(SimpleTestCase):
    def _call(self, host):
        request = RequestFactory().get("/", HTTP_HOST=host, HTTP_X_FORWARDED_PROTO="http")
        return PublicSchemeMiddleware(_absolute_url)(request).content.decode()

    @override_settings(EVILFLOWERS_BASE_URL="https://catalog.example.com")
    def test_public_host_is_upgraded_to_https(self):
        self.assertEqual(self._call("catalog.example.com"), "https://catalog.example.com/x")

    @override_settings(EVILFLOWERS_BASE_URL="https://catalog.example.com")
    def test_other_host_is_left_alone(self):
        self.assertEqual(self._call("evilflowers:8000"), "http://evilflowers:8000/x")

    @override_settings(EVILFLOWERS_BASE_URL="http://localhost:8000")
    def test_loopback_base_url_is_a_no_op(self):
        self.assertEqual(self._call("localhost:8000"), "http://localhost:8000/x")

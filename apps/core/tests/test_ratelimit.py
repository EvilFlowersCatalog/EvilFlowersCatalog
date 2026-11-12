"""Request rate limits (#21, IP-026)."""

import base64

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.core import ratelimit
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, User

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "ratelimit-tests"}}
LIMITS = dict(
    CACHES=LOCMEM,
    EVILFLOWERS_RATE_LIMIT_ENABLED=True,
    EVILFLOWERS_RATE_LIMIT_WINDOW=3600,
    EVILFLOWERS_RATE_LIMIT_ANONYMOUS=3,
    EVILFLOWERS_RATE_LIMIT_AUTHENTICATED=5,
    EVILFLOWERS_RATE_LIMIT_KEY="user",
    EVILFLOWERS_RATE_LIMIT_EXEMPT_SUPERUSERS=True,
    EVILFLOWERS_RATE_LIMIT_PROXY_COUNT=1,
)


class ClientIpTests(SimpleTestCase):
    def _ip(self, forwarded=None, remote="10.0.0.1", proxies=1):
        extra = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            extra["HTTP_X_FORWARDED_FOR"] = forwarded
        with override_settings(EVILFLOWERS_RATE_LIMIT_PROXY_COUNT=proxies):
            return ratelimit.client_ip(RequestFactory().get("/", **extra))

    def test_the_proxy_appended_entry_not_the_client_written_one(self):
        # The client wrote 1.1.1.1 itself; the ingress appended the real peer 147.175.1.2.
        self.assertEqual(self._ip("1.1.1.1, 147.175.1.2"), "147.175.1.2")
        self.assertEqual(self._ip("1.1.1.1, 147.175.1.2, 10.42.0.5", proxies=2), "147.175.1.2")

    def test_no_proxy_or_short_header_uses_the_socket(self):
        self.assertEqual(self._ip("1.1.1.1", proxies=0), "10.0.0.1")
        self.assertEqual(self._ip("1.1.1.1", proxies=2), "10.0.0.1")
        self.assertEqual(self._ip(None), "10.0.0.1")
        self.assertEqual(self._ip("not-an-ip"), "10.0.0.1")


@override_settings(**LIMITS)
class RateLimitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = cls._user(source, "reader")
        cls.admin = cls._user(source, "admin", is_superuser=True)

    @staticmethod
    def _user(source, username, **extra) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=source, **extra)
        user.set_password("Original-Pass-42")
        user.save()
        return user

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _bearer(self, user, key=None) -> dict:
        key = key or ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def test_anonymous_budget_with_headers_then_429(self):
        for remaining in (2, 1, 0):
            response = self.client.get("/api/v1/languages", REMOTE_ADDR="147.175.1.2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["X-RateLimit-Limit"], "3")
            self.assertEqual(response["X-RateLimit-Remaining"], str(remaining))
        refused = self.client.get("/api/v1/languages", REMOTE_ADDR="147.175.1.2")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json()["type"], "/rate-limited")
        self.assertGreater(int(refused["Retry-After"]), 0)
        # Another address has its own budget.
        self.assertEqual(self.client.get("/api/v1/languages", REMOTE_ADDR="147.175.9.9").status_code, 200)

    def test_a_forged_forwarded_for_does_not_buy_a_new_budget(self):
        for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            self.client.get("/api/v1/languages", HTTP_X_FORWARDED_FOR=f"{forged}, 147.175.1.2")
        response = self.client.get("/api/v1/languages", HTTP_X_FORWARDED_FOR="4.4.4.4, 147.175.1.2")
        self.assertEqual(response.status_code, 429)

    def test_signed_in_users_count_per_account_across_api_keys(self):
        first, second = (ApiKey.objects.create(user=self.reader, name=name) for name in ("a", "b"))
        for key in (first, second, first, second, first):
            self.assertEqual(
                self.client.get("/api/v1/users/me", headers=self._bearer(self.reader, key)).status_code, 200
            )
        self.assertEqual(
            self.client.get("/api/v1/users/me", headers=self._bearer(self.reader, second)).status_code, 429
        )

    @override_settings(EVILFLOWERS_RATE_LIMIT_KEY="api_key")
    def test_per_api_key_when_configured(self):
        first, second = (ApiKey.objects.create(user=self.reader, name=name) for name in ("a", "b"))
        for _ in range(5):
            self.client.get("/api/v1/users/me", headers=self._bearer(self.reader, first))
        self.assertEqual(
            self.client.get("/api/v1/users/me", headers=self._bearer(self.reader, first)).status_code, 429
        )
        self.assertEqual(
            self.client.get("/api/v1/users/me", headers=self._bearer(self.reader, second)).status_code, 200
        )

    def test_superusers_are_exempt_by_default(self):
        for _ in range(7):
            response = self.client.get("/api/v1/users/me", headers=self._bearer(self.admin))
            self.assertEqual(response.status_code, 200)
        self.assertFalse(response.has_header("X-RateLimit-Limit"))

    def test_wrong_passwords_are_counted_against_the_address(self):
        wrong = "Basic " + base64.b64encode(b"reader:guess").decode()
        statuses = [
            self.client.get("/api/v1/users/me", HTTP_AUTHORIZATION=wrong, REMOTE_ADDR="147.175.1.2").status_code
            for _ in range(5)
        ]
        self.assertEqual(statuses[:3], [401, 401, 401])
        self.assertEqual(statuses[3:], [429, 429])

    def test_range_continuations_are_not_counted(self):
        for _ in range(5):
            response = self.client.get("/api/v1/languages", HTTP_RANGE="bytes=65536-131071")
            self.assertEqual(response.status_code, 200)
        self.assertFalse(response.has_header("X-RateLimit-Limit"))

    @override_settings(EVILFLOWERS_RATE_LIMIT_ENABLED=False)
    def test_off_by_default_switch(self):
        for _ in range(5):
            response = self.client.get("/api/v1/languages")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.has_header("X-RateLimit-Limit"))

    def test_an_unreachable_cache_lets_requests_through(self):
        from unittest import mock

        with mock.patch("apps.core.ratelimit.cache.add", side_effect=ConnectionError("redis down")):
            self.assertEqual(self.client.get("/api/v1/languages").status_code, 200)

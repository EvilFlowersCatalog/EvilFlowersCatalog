"""#198: an LCP passphrase must be at least `EVILFLOWERS_LCP_PASSPHRASE_MIN_LENGTH` (8) characters."""

import json

from django.conf import settings
from django.test import TestCase

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, User


class LcpPassphraseLengthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="Reader", surname="T", auth_source=source)
        cls.reader.set_password("original-password")
        cls.reader.save()

    def _put(self, payload):
        key = ApiKey.objects.create(user=self.reader, name="test")
        return self.client.put(
            f"/api/v1/users/{self.reader.pk}",
            data=json.dumps({"name": "Reader", "surname": "T", **payload}),
            content_type="application/json",
            headers={"Authorization": f"Bearer {JWTFactory(self.reader.pk).api_key(str(key.pk))}"},
        )

    def test_default_minimum_is_eight(self):
        self.assertEqual(settings.EVILFLOWERS_LCP_PASSPHRASE_MIN_LENGTH, 8)

    def test_short_passphrase_is_refused(self):
        response = self._put({"lcp_passphrase": "abcd"})
        self.assertEqual(response.status_code, 422, response.content[:300])
        self.reader.refresh_from_db()
        self.assertIsNone(self.reader.lcp_passphrase_hash)

    def test_seven_characters_are_refused(self):
        response = self._put({"lcp_passphrase": "abcdefg"})
        self.assertEqual(response.status_code, 422, response.content[:300])

    def test_eight_characters_are_accepted_and_hashed(self):
        response = self._put({"lcp_passphrase": "abcdefgh"})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.reader.refresh_from_db()
        self.assertEqual(len(self.reader.lcp_passphrase_hash), 64)

    def test_profile_update_without_passphrase_still_works(self):
        response = self._put({"lcp_passphrase_hint": "my dog"})
        self.assertEqual(response.status_code, 200, response.content[:300])

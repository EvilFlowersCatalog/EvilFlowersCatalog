"""Bearer authentication stays cheap (#125) and covers skip it entirely (#126)."""

import base64
import datetime
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core import auth
from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, User

# 1×1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class JwtKeyCacheTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.user = User(username="reader", name="Re", surname="Ader", auth_source=source)
        cls.user.set_unusable_password()
        cls.user.save()

    def test_the_jwk_is_imported_once_for_many_tokens(self):
        auth._jwt_key.cache_clear()
        auth._jwt_verify_key.cache_clear()
        with patch.object(auth, "import_key", wraps=auth.import_key) as import_key:
            for _ in range(5):
                claims = JWTFactory.decode(JWTFactory(self.user.pk).access())
                self.assertEqual(claims["sub"], str(self.user.pk))
        # One import of the private key, one of its public half.
        self.assertEqual(import_key.call_count, 2)

    def test_verification_uses_only_the_public_key(self):
        self.assertFalse(auth._jwt_verify_key().is_private)

    def test_api_key_last_seen_is_written_at_most_once_a_minute(self):
        key = ApiKey.objects.create(user=self.user, name="test")
        headers = {"Authorization": f"Bearer {JWTFactory(self.user.pk).api_key(str(key.pk))}"}

        self.client.get("/api/v1/users/me", headers=headers)
        key.refresh_from_db()
        first = key.last_seen_at
        self.assertIsNotNone(first)

        with CaptureQueriesContext(connection) as queries:
            self.client.get("/api/v1/users/me", headers=headers)
        self.assertFalse([q for q in queries if q["sql"].startswith('UPDATE "api_keys"')])
        key.refresh_from_db()
        self.assertEqual(key.last_seen_at, first)

        ApiKey.objects.filter(pk=key.pk).update(last_seen_at=timezone.now() - datetime.timedelta(minutes=2))
        self.client.get("/api/v1/users/me", headers=headers)
        key.refresh_from_db()
        self.assertGreater(key.last_seen_at, first)


class CoverWithoutAuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        owner = User(username="owner", name="O", surname="W", auth_source=source)
        owner.set_unusable_password()
        owner.save()
        catalog = Catalog.objects.create(creator=owner, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(creator=owner, catalog=catalog, title="No cover")

    def test_a_thumbnail_is_served_in_one_query(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root):
            self.entry.thumbnail.save("thumb.png", ContentFile(PNG), save=False)
            Entry.objects.filter(pk=self.entry.pk).update(
                thumbnail=self.entry.thumbnail.name, thumbnail_mime="image/png"
            )
            url = reverse("files:thumbnail-download", kwargs={"entry_id": self.entry.pk})
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"{url}?access_token=whatever")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PNG)
            # The entry row, narrowed by `only()`; no user lookup, no deferred-field reload.
            self.assertEqual(len(queries), 1, [q["sql"] for q in queries])
            self.assertIn("Last-Modified", response.headers)

            since = response["Last-Modified"]
            self.assertEqual(self.client.get(url, headers={"If-Modified-Since": since}).status_code, 304)

    def test_a_thumbnail_ignores_the_token_it_is_sent(self):
        url = reverse("files:thumbnail-download", kwargs={"entry_id": self.entry.pk})
        with patch.object(auth.JWTFactory, "decode") as decode:
            response = self.client.get(f"{url}?access_token=whatever")
        decode.assert_not_called()
        # No thumbnail on this entry: a 404, not a 401 for the unparseable token.
        self.assertEqual(response.status_code, 404)

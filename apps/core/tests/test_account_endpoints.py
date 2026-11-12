"""Regressions for two REST account defects (IP-014 Q18).

1. `GET /api/v1/api_keys` re-issued every listed key's bearer token. A token is
   now returned by `POST /api/v1/api_keys` only.
2. `PUT`/`DELETE /api/v1/users/{id}` let a non-superuser holding
   `core.change_user` / `core.delete_user` act on a superuser — reset their
   password and log in as them. And `_get_user` answered 404 for a missing id
   but 403 for a real one, confirming which ids are accounts.
"""

import json
import uuid

from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, User


class AccountEndpointTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.root = cls._user("root", is_superuser=True)
        cls.admin = cls._user("useradmin")
        cls.admin.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="core",
                codename__in=["view_user", "add_user", "change_user", "delete_user"],
            )
        )
        cls.reader = cls._user("reader")

    @classmethod
    def _user(cls, username, **kwargs) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=cls.source, **kwargs)
        user.set_password("original-password")
        user.save()
        return user

    def _headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def _put(self, user, target, payload):
        return self.client.put(
            f"/api/v1/users/{target.pk}",
            data=json.dumps(payload),
            content_type="application/json",
            headers=self._headers(user),
        )


class ApiKeyTokenTests(AccountEndpointTestCase):
    def test_creation_returns_the_token(self):
        response = self.client.post(
            "/api/v1/api_keys",
            data=json.dumps({"name": "laptop"}),
            content_type="application/json",
            headers=self._headers(self.reader),
        )
        self.assertEqual(response.status_code, 201, response.content[:300])
        body = response.json()["response"]
        self.assertEqual(JWTFactory.decode(body["token"])["jti"], body["id"])

    def test_the_listing_never_returns_a_token(self):
        response = self.client.get("/api/v1/api_keys", headers=self._headers(self.root))
        self.assertEqual(response.status_code, 200, response.content[:300])
        items = response.json()["items"]
        self.assertTrue(items)
        for item in items:
            self.assertNotIn("token", item)


class UserAdministrationTests(AccountEndpointTestCase):
    def test_a_user_administrator_cannot_reset_a_superusers_password(self):
        response = self._put(self.admin, self.root, {"name": "Root", "surname": "T", "password": "taken-over"})
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password("original-password"))

    def test_a_user_administrator_cannot_delete_a_superuser(self):
        response = self.client.delete(f"/api/v1/users/{self.root.pk}", headers=self._headers(self.admin))
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertTrue(User.objects.filter(pk=self.root.pk).exists())

    def test_a_user_administrator_can_still_view_a_superuser(self):
        response = self.client.get(f"/api/v1/users/{self.root.pk}", headers=self._headers(self.admin))
        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_a_user_administrator_can_still_manage_ordinary_users(self):
        response = self._put(self.admin, self.reader, {"name": "Renamed", "surname": "T"})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.reader.refresh_from_db()
        self.assertEqual(self.reader.name, "Renamed")

    def test_a_superuser_can_reset_a_superusers_password(self):
        other_root = self._user("root2", is_superuser=True)
        response = self._put(self.root, other_root, {"name": "R", "surname": "T", "password": "reset-by-root"})
        self.assertEqual(response.status_code, 200, response.content[:300])
        other_root.refresh_from_db()
        self.assertTrue(other_root.check_password("reset-by-root"))

    def test_a_user_can_still_edit_themselves(self):
        response = self._put(self.reader, self.reader, {"name": "Me", "surname": "T"})
        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_a_hidden_account_looks_exactly_like_a_missing_one(self):
        headers = self._headers(self.reader)
        real = self.client.get(f"/api/v1/users/{self.root.pk}", headers=headers)
        fake = self.client.get(f"/api/v1/users/{uuid.uuid4()}", headers=headers)
        self.assertEqual((real.status_code, fake.status_code), (404, 404))

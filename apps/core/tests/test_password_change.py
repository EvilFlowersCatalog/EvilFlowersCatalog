"""Changing one's own password, and the flag that requires it (#8)."""

import json

from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, User


class PasswordChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.database = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.directory = AuthSource.objects.create(name="ldap", driver=AuthSource.Driver.LDAP)
        cls.admin = cls._user("useradmin", cls.database)
        cls.admin.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="core", codename__in=["view_user", "change_user"])
        )
        cls.reader = cls._user("reader", cls.database)
        cls.ldap_reader = cls._user("ldapreader", cls.directory)

    @classmethod
    def _user(cls, username, source) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=source)
        user.set_password("Original-Pass-42")
        user.save()
        return user

    def _headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def _change(self, user, current, new):
        return self.client.post(
            "/api/v1/users/me/password",
            data=json.dumps({"current_password": current, "new_password": new}),
            content_type="application/json",
            headers=self._headers(user),
        )

    def test_change_with_the_current_password(self):
        response = self._change(self.reader, "Original-Pass-42", "Brand-New-Secret-7")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.reader.refresh_from_db()
        self.assertTrue(self.reader.check_password("Brand-New-Secret-7"))

    def test_wrong_current_password_is_refused(self):
        self.assertEqual(self._change(self.reader, "guess", "Brand-New-Secret-7").status_code, 403)

    def test_weak_or_unchanged_password_is_refused(self):
        self.assertEqual(self._change(self.reader, "Original-Pass-42", "123").status_code, 422)
        self.assertEqual(self._change(self.reader, "Original-Pass-42", "Original-Pass-42").status_code, 422)

    def test_directory_accounts_change_it_in_the_directory(self):
        self.assertEqual(self._change(self.ldap_reader, "Original-Pass-42", "Brand-New-Secret-7").status_code, 409)

    def test_the_flag_blocks_everything_but_me_and_the_change(self):
        response = self.client.put(
            f"/api/v1/users/{self.reader.pk}",
            data=json.dumps({"name": "Reader", "surname": "T", "password_change_required": True}),
            content_type="application/json",
            headers=self._headers(self.admin),
        )
        self.assertEqual(response.status_code, 200, response.content[:300])

        blocked = self.client.get("/api/v1/catalogs", headers=self._headers(self.reader))
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["type"], "/password-change-required")

        me = self.client.get("/api/v1/users/me", headers=self._headers(self.reader))
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["response"]["password_change_required"])

        self.assertEqual(self._change(self.reader, "Original-Pass-42", "Brand-New-Secret-7").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/catalogs", headers=self._headers(self.reader)).status_code, 200)

    def test_a_flagged_user_can_still_sign_in_and_learns_about_it(self):
        User.objects.filter(pk=self.reader.pk).update(password_change_required=True)
        response = self.client.post(
            "/api/v1/token",
            data=json.dumps({"username": "reader", "password": "Original-Pass-42"}),
            content_type="application/json",
            # A stale header from an earlier session must not lock the user out of signing in.
            headers=self._headers(self.reader),
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertTrue(response.json()["response"]["user"]["password_change_required"])

    def test_a_user_cannot_lift_the_flag_for_themselves(self):
        self.reader.password_change_required = False
        self.reader.save()
        response = self.client.put(
            f"/api/v1/users/{self.reader.pk}",
            data=json.dumps({"name": "Reader", "surname": "T", "password_change_required": True}),
            content_type="application/json",
            headers=self._headers(self.reader),
        )
        self.assertEqual(response.status_code, 200)
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.password_change_required)

"""User and API-key tools, and the access boundary around them.

Four roles matter here: an ordinary user (sees and edits only themselves), a
*user administrator* holding `core.*_user` permissions without superuser rights,
a superuser, and anonymous. Most of these tests pin down what the middle role
may not do — that is where an escalation would hide.
"""

import json
import uuid

from django.contrib.auth.models import Permission
from django.test import override_settings

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, User, UserCatalog
from apps.mcp.protocol import LATEST_PROTOCOL_VERSION, PROTOCOL_VERSION_HEADER
from apps.mcp.tests.test_management import ManagementTestCase


class AccountsTestCase(ManagementTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = cls._user("useradmin")
        cls.admin.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="core",
                codename__in=["view_user", "add_user", "change_user", "delete_user"],
            )
        )
        cls.reader_key = ApiKey.objects.create(user=cls.reader, name="reader laptop")
        cls.manager_key = ApiKey.objects.create(user=cls.manager, name="manager laptop")

    def setUp(self):
        super().setUp()
        # Permissions are cached per instance; the class attribute outlives tests.
        self.admin = User.objects.get(pk=self.admin.pk)


class UserVisibilityTests(AccountsTestCase):
    def test_an_ordinary_user_lists_only_themselves(self):
        payload = self.ok("list_users", {}, self.reader)
        self.assertEqual([item["username"] for item in payload["items"]], ["reader"])

    def test_a_user_administrator_lists_everyone(self):
        payload = self.ok("list_users", {}, self.admin)
        self.assertEqual(payload["metadata"]["total"], User.objects.count())

    def test_list_filters_by_catalog_membership(self):
        payload = self.ok("list_users", {"catalog_id": str(self.catalog.pk)}, self.superuser)
        self.assertEqual({item["username"] for item in payload["items"]}, {"manager", "reader"})

    def test_get_user_defaults_to_the_caller(self):
        payload = self.ok("get_user", {}, self.reader)
        self.assertEqual(payload["user"]["username"], "reader")
        self.assertEqual(payload["user"]["active_api_keys"], 1)
        self.assertEqual([grant["mode"] for grant in payload["user"]["catalogs"]], ["read"])

    def test_another_users_account_is_reported_as_absent_not_forbidden(self):
        real, fake = str(self.manager.pk), str(uuid.uuid4())
        message = self.refused("get_user", {"user_id": real}, self.reader)
        missing = self.refused("get_user", {"user_id": fake}, self.reader)

        # Indistinguishable from an id that really does not exist.
        self.assertIn("does not exist", message)
        self.assertEqual(message.replace(real, "ID"), missing.replace(fake, "ID"))

    def test_catalog_grants_are_limited_to_catalogs_the_caller_can_read(self):
        UserCatalog.objects.create(user=self.manager, catalog=self.other_catalog, mode=UserCatalog.Mode.READ)

        payload = self.ok("get_user", {"user_id": str(self.manager.pk)}, self.admin)
        self.assertEqual([grant["url_name"] for grant in payload["user"]["catalogs"]], [])
        # And a user administrator does not see someone else's key count.
        self.assertNotIn("active_api_keys", payload["user"])

        payload = self.ok("get_user", {"user_id": str(self.manager.pk)}, self.superuser)
        self.assertEqual(sorted(grant["url_name"] for grant in payload["user"]["catalogs"]), ["elsewhere", "managed"])
        self.assertEqual(payload["user"]["active_api_keys"], 1)


class UserCreationTests(AccountsTestCase):
    def test_a_user_administrator_creates_an_api_key_only_account(self):
        payload = self.ok("create_user", {"username": "Agent-Bot", "name": "Agent", "surname": "Bot"}, self.admin)[
            "user"
        ]

        self.assertEqual(payload["username"], "agent-bot")
        self.assertFalse(payload["has_password"])
        self.assertEqual(payload["catalogs"], [])
        self.assertFalse(User.objects.get(username="agent-bot").has_usable_password())

    def test_a_password_is_hashed(self):
        self.ok(
            "create_user",
            {"username": "person", "name": "P", "surname": "Q", "password": "long-enough-pass"},
            self.admin,
        )
        self.assertTrue(User.objects.get(username="person").check_password("long-enough-pass"))

    def test_an_ordinary_user_may_not_create_users(self):
        message = self.refused("create_user", {"username": "x", "name": "X", "surname": "Y"}, self.manager)
        self.assertIn("restricted to administrators", message)
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_a_user_administrator_may_not_create_a_superuser(self):
        message = self.refused(
            "create_user", {"username": "boss", "name": "B", "surname": "S", "is_superuser": True}, self.admin
        )
        self.assertIn("superuser", message)
        self.assertFalse(User.objects.filter(username="boss").exists())

    def test_a_duplicate_username_is_a_conflict_regardless_of_case(self):
        message = self.refused("create_user", {"username": "READER", "name": "R", "surname": "R"}, self.admin)
        self.assertIn("already exists", message)

    def test_a_short_password_is_refused(self):
        message = self.refused(
            "create_user", {"username": "weak", "name": "W", "surname": "P", "password": "short"}, self.admin
        )
        self.assertIn("8 characters", message)


class UserUpdateTests(AccountsTestCase):
    def test_a_user_may_edit_their_own_profile(self):
        payload = self.ok("update_user", {"user_id": str(self.reader.pk), "surname": "Renamed"}, self.reader)
        self.assertEqual(payload["user"]["surname"], "Renamed")

    def test_a_user_may_not_grant_themselves_the_administrator_role(self):
        message = self.refused("update_user", {"user_id": str(self.reader.pk), "is_superuser": True}, self.reader)
        self.assertIn("restricted to administrators", message)
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.is_superuser)

    def test_a_superuser_may_not_demote_themselves(self):
        message = self.refused(
            "update_user", {"user_id": str(self.superuser.pk), "is_superuser": False}, self.superuser
        )
        self.assertIn("own role", message)

    def test_nobody_may_deactivate_themselves(self):
        message = self.refused("update_user", {"user_id": str(self.admin.pk), "is_active": False}, self.admin)
        self.assertIn("your own account", message)

    def test_nobody_may_set_their_own_password_through_an_agent(self):
        message = self.refused(
            "update_user", {"user_id": str(self.reader.pk), "password": "a-new-password"}, self.reader
        )
        self.assertIn("portal", message)

    def test_an_ordinary_user_cannot_see_let_alone_edit_someone_else(self):
        message = self.refused("update_user", {"user_id": str(self.manager.pk), "name": "Hacked"}, self.reader)
        self.assertIn("does not exist", message)
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.name, "Manager")

    def test_a_user_administrator_may_deactivate_and_reset_an_ordinary_user(self):
        self.ok(
            "update_user",
            {"user_id": str(self.reader.pk), "is_active": False, "password": "reset-by-admin"},
            self.admin,
        )
        self.reader.refresh_from_db()
        self.assertFalse(self.reader.is_active)
        self.assertTrue(self.reader.check_password("reset-by-admin"))

    def test_a_user_administrator_may_not_touch_a_superuser(self):
        # Without this, `core.change_user` resets an administrator's password.
        message = self.refused(
            "update_user", {"user_id": str(self.superuser.pk), "password": "taken-over!"}, self.admin
        )
        self.assertIn("superuser", message)
        self.superuser.refresh_from_db()
        self.assertFalse(self.superuser.has_usable_password())

    def test_a_user_administrator_may_not_change_roles(self):
        message = self.refused("update_user", {"user_id": str(self.reader.pk), "is_superuser": True}, self.admin)
        self.assertIn("superuser", message)

    def test_a_superuser_may_promote_someone_else(self):
        payload = self.ok("update_user", {"user_id": str(self.reader.pk), "is_superuser": True}, self.superuser)
        self.assertTrue(payload["user"]["is_superuser"])


class UserDeletionTests(AccountsTestCase):
    def test_a_user_administrator_deletes_with_confirmation(self):
        payload = self.ok("delete_user", {"user_id": str(self.reader.pk), "confirm_username": "reader"}, self.admin)
        self.assertTrue(payload["deleted"])
        self.assertFalse(User.objects.filter(pk=self.reader.pk).exists())
        self.assertFalse(ApiKey.objects.filter(pk=self.reader_key.pk).exists())

    def test_a_wrong_confirmation_deletes_nothing(self):
        message = self.refused(
            "delete_user", {"user_id": str(self.reader.pk), "confirm_username": "someone"}, self.admin
        )
        self.assertIn("Nothing has been deleted", message)
        self.assertTrue(User.objects.filter(pk=self.reader.pk).exists())

    def test_nobody_may_delete_themselves(self):
        message = self.refused(
            "delete_user", {"user_id": str(self.superuser.pk), "confirm_username": "root"}, self.superuser
        )
        self.assertIn("your own account", message)

    def test_a_user_administrator_may_not_delete_a_superuser(self):
        message = self.refused(
            "delete_user", {"user_id": str(self.superuser.pk), "confirm_username": "root"}, self.admin
        )
        self.assertIn("superuser", message)
        self.assertTrue(User.objects.filter(pk=self.superuser.pk).exists())

    def test_the_permission_check_runs_before_the_lookup(self):
        # An ordinary user learns nothing, not even whether the id is real.
        message = self.refused("delete_user", {"user_id": str(uuid.uuid4()), "confirm_username": "x"}, self.reader)
        self.assertIn("restricted to administrators", message)


class ApiKeyToolTests(AccountsTestCase):
    def test_any_user_may_mint_a_key_for_themselves(self):
        payload = self.ok("create_api_key", {"name": "Claude Desktop"}, self.reader)

        key = ApiKey.objects.get(pk=payload["api_key"]["id"])
        self.assertEqual(key.user, self.reader)
        self.assertEqual(payload["authorization_header"], f"Bearer {payload['token']}")
        self.assertEqual(JWTFactory.decode(payload["token"])["jti"], str(key.pk))

    def test_the_listing_never_carries_a_token(self):
        self.ok("create_api_key", {"name": "Claude Desktop"}, self.reader)
        payload = self.ok("list_api_keys", {}, self.reader)

        self.assertEqual(len(payload["items"]), 2)
        for item in payload["items"]:
            self.assertNotIn("token", item)
        self.assertNotIn("eyJ", json.dumps(payload))

    def test_a_user_lists_only_their_own_keys(self):
        payload = self.ok("list_api_keys", {"user_id": str(self.manager.pk)}, self.reader)
        self.assertEqual(payload["items"], [])

    def test_a_superuser_lists_everyones_keys(self):
        payload = self.ok("list_api_keys", {}, self.superuser)
        self.assertEqual({item["username"] for item in payload["items"]}, {"reader", "manager"})

    def test_minting_a_key_for_someone_else_needs_a_superuser(self):
        for caller in (self.reader, self.admin):
            with self.subTest(caller=caller.username):
                message = self.refused(
                    "create_api_key", {"name": "impersonate", "user_id": str(self.manager.pk)}, caller
                )
                self.assertIn("restricted to administrators", message)
        self.assertFalse(ApiKey.objects.filter(name="impersonate").exists())

    def test_a_superuser_provisions_a_service_account_key(self):
        bot = self.ok("create_user", {"username": "bot", "name": "B", "surname": "Ot"}, self.superuser)["user"]
        payload = self.ok("create_api_key", {"name": "ingest", "user_id": bot["id"]}, self.superuser)
        self.assertEqual(payload["api_key"]["user_id"], bot["id"])

    def test_no_key_is_minted_for_a_deactivated_account(self):
        User.objects.filter(pk=self.reader.pk).update(is_active=False)
        message = self.refused("create_api_key", {"name": "x", "user_id": str(self.reader.pk)}, self.superuser)
        self.assertIn("deactivated", message)

    def test_a_user_revokes_their_own_key(self):
        payload = self.ok("revoke_api_key", {"api_key_id": str(self.reader_key.pk)}, self.reader)
        self.assertTrue(payload["revoked"])
        self.reader_key.refresh_from_db()
        self.assertFalse(self.reader_key.is_active)

        again = self.ok("revoke_api_key", {"api_key_id": str(self.reader_key.pk)}, self.reader)
        self.assertFalse(again["revoked"])

    def test_someone_elses_key_is_reported_as_absent(self):
        for caller in (self.reader, self.admin):
            with self.subTest(caller=caller.username):
                message = self.refused("revoke_api_key", {"api_key_id": str(self.manager_key.pk)}, caller)
                self.assertIn("does not exist", message)
        self.manager_key.refresh_from_db()
        self.assertTrue(self.manager_key.is_active)

    def test_a_superuser_revokes_anyones_key(self):
        self.ok("revoke_api_key", {"api_key_id": str(self.manager_key.pk)}, self.superuser)
        self.manager_key.refresh_from_db()
        self.assertFalse(self.manager_key.is_active)

    @override_settings(EVILFLOWERS_MCP_ALLOW_WRITE=False)
    def test_minting_is_withdrawn_with_the_write_surface(self):
        response = self.call("create_api_key", {"name": "x"}, self.reader)
        self.assertIn("Unknown tool", response["error"]["message"])


class ApiKeyLifecycleOverHttpTests(AccountsTestCase):
    """Through the real endpoint and `BearerBackend`, which is what sets `request.api_key`."""

    def _call(self, name, arguments, token):
        response = self.client.post(
            "/mcp/v1",
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            ),
            content_type="application/json",
            headers={PROTOCOL_VERSION_HEADER: LATEST_PROTOCOL_VERSION, "Authorization": f"Bearer {token}"},
        )
        return response

    def test_a_minted_key_works_is_marked_current_and_dies_when_revoked(self):
        bootstrap = JWTFactory(self.reader.pk).api_key(str(self.reader_key.pk))
        created = json.loads(self._call("create_api_key", {"name": "second"}, bootstrap).content)
        token = created["result"]["structuredContent"]["token"]
        new_id = created["result"]["structuredContent"]["api_key"]["id"]

        listing = json.loads(self._call("list_api_keys", {}, token).content)["result"]["structuredContent"]
        current = {item["id"]: item["is_current"] for item in listing["items"]}
        self.assertEqual(current, {new_id: True, str(self.reader_key.pk): False})

        revoked = json.loads(self._call("revoke_api_key", {"api_key_id": new_id}, token).content)
        self.assertTrue(revoked["result"]["structuredContent"]["api_key"]["is_current"])

        self.assertEqual(self._call("whoami", {}, token).status_code, 401)
        self.assertEqual(self._call("whoami", {}, bootstrap).status_code, 200)

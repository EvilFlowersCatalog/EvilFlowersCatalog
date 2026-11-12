"""IP-025: e-mail import from LDAP — at login and in the background sync.

The directory is mocked; what is pinned down here is the contact rule (a
directory address never demotes one the user chose) and that the sync asks
for attributes by name — ldap.stuba.sk filters `*` for anonymous binds.
"""

import base64
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.core.auth import BasicBackend
from apps.core.models import AuthSource, User
from apps.core.services.ldap_directory import requested_attributes, sync_users
from apps.notifications.models import NotificationContact
from apps.notifications.services import NotificationService

CONFIG = {
    "URI": "ldap://ldap.example.org",
    "BIND": "uid={username},ou=People,dc=stuba,dc=SK",
    "FILTER": "(uid={username})",
    "ROOT_DN": "ou=People,dc=stuba,dc=SK",
    "GROUP_MAP": {},
    "SUPERADMIN_GROUP": None,
    "USER_ATTR_MAP": {"name": "givenName", "surname": "sn"},
    "NOTIFICATION_CONTACT_MAP": {"email": "mail"},
}
EMAIL = NotificationContact.ContactType.EMAIL
LDAP = NotificationContact.Source.LDAP


def directory(entries: dict):
    """A fake LDAP connection answering `(uid=…)` searches from `entries` (username → attrs)."""
    connection = MagicMock()
    searches = []

    def search(base, scope, filterstr, attributes):
        searches.append(attributes)
        username = filterstr[len("(uid=") : -1]
        return username

    def result(message_id, timeout):
        attrs = entries.get(message_id)
        if attrs is None:
            return (101, [])
        return (101, [(f"uid={message_id},ou=People,dc=stuba,dc=sk", attrs)])

    connection.search.side_effect = search
    connection.result.side_effect = result
    connection.searches = searches
    return connection


def ldap_attrs(mail=None, given="Alena", sn="Nováková"):
    attrs = {"givenName": [given.encode()], "sn": [sn.encode()]}
    if mail:
        attrs["mail"] = [mail.encode()]
    return attrs


class DirectoryContactRuleTests(TestCase):
    def setUp(self):
        source = AuthSource.objects.create(name="stuba.sk", driver=AuthSource.Driver.LDAP, content=CONFIG)
        self.user = User.objects.create(username="xnovakova", auth_source=source)

    def contacts(self):
        return list(
            NotificationContact.objects.filter(user=self.user)
            .order_by("value")
            .values_list("value", "is_primary", "source")
        )

    def test_created_as_primary_when_the_user_has_none(self):
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk")
        self.assertEqual((change.action, change.is_primary), ("created", True))
        self.assertEqual(self.contacts(), [("alena@stuba.sk", True, LDAP)])

    def test_never_demotes_an_address_the_user_chose(self):
        NotificationService.set_contact(self.user, EMAIL, "alena@gmail.com")
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk")
        self.assertEqual((change.action, change.is_primary), ("created", False))
        self.assertEqual(NotificationService.resolve_recipient_email(self.user), "alena@gmail.com")

    def test_directory_change_rewrites_its_own_row(self):
        NotificationService.sync_directory_contact(self.user, EMAIL, "old@stuba.sk")
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "new@stuba.sk")
        self.assertEqual(change.action, "updated")
        self.assertEqual(self.contacts(), [("new@stuba.sk", True, LDAP)])

    def test_adopts_a_row_imported_before_the_source_field_existed(self):
        NotificationContact.objects.create(user=self.user, type=EMAIL, value="alena@stuba.sk", is_primary=True)
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk")
        self.assertEqual(change.action, "adopted")
        self.assertEqual(self.contacts(), [("alena@stuba.sk", True, LDAP)])

    def test_adoption_ignores_case(self):
        NotificationContact.objects.create(
            user=self.user, type=EMAIL, value="Alena.Novakova@stuba.sk", is_primary=True
        )
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "alena.novakova@stuba.sk")
        self.assertEqual(change.action, "adopted")
        self.assertEqual(self.contacts(), [("alena.novakova@stuba.sk", True, LDAP)])

    def test_second_run_is_unchanged(self):
        NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk")
        self.assertEqual(
            NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk").action, "unchanged"
        )

    def test_dry_run_writes_nothing(self):
        change = NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk", dry_run=True)
        self.assertEqual(change.action, "created")
        self.assertEqual(self.contacts(), [])

    def test_user_with_two_addresses_no_longer_breaks(self):
        # The old login code: update_or_create(user=, type=) → MultipleObjectsReturned.
        NotificationService.set_contact(self.user, EMAIL, "a@gmail.com")
        NotificationService.set_contact(self.user, EMAIL, "b@gmail.com", is_primary=False)
        NotificationService.sync_directory_contact(self.user, EMAIL, "alena@stuba.sk")
        self.assertEqual(NotificationService.resolve_recipient_email(self.user), "a@gmail.com")


class DirectorySyncTests(TestCase):
    def setUp(self):
        self.source = AuthSource.objects.create(name="stuba.sk", driver=AuthSource.Driver.LDAP, content=CONFIG)
        self.alena = User.objects.create(username="xnovakova", auth_source=self.source)
        self.gone = User.objects.create(username="xgone", auth_source=self.source)
        database = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        self.local = User.objects.create(username="local", auth_source=database)

    def run_sync(self, entries, **kwargs):
        connection = directory(entries)
        with patch("apps.core.services.ldap_directory.ldap_initialize", return_value=connection):
            return sync_users(self.source, **kwargs), connection

    def test_backfills_users_who_never_logged_in_again(self):
        report, connection = self.run_sync({"xnovakova": ldap_attrs("Alena.Novakova@stuba.sk")})

        self.assertEqual(report.bind, "anonymous")
        connection.simple_bind_s.assert_called_once_with("", "")
        self.assertEqual(report.summary, {"users": 2, "changed": 1, "unchanged": 0, "not_found": 1, "errors": 0})
        self.assertEqual(NotificationService.resolve_recipient_email(self.alena), "alena.novakova@stuba.sk")
        self.alena.refresh_from_db()
        self.assertEqual((self.alena.name, self.alena.surname), ("Alena", "Nováková"))
        # A user the directory no longer has is reported and left alone.
        self.assertTrue(User.objects.get(pk=self.gone.pk).is_active)

    def test_asks_for_attributes_by_name_never_star(self):
        _report, connection = self.run_sync({"xnovakova": ldap_attrs("a@stuba.sk")})
        for attributes in connection.searches:
            self.assertNotIn("*", attributes)
            self.assertIn("mail", attributes)
        self.assertEqual(requested_attributes(CONFIG), ["givenName", "mail", "memberOf", "sn"])

    def test_uses_the_proxy_account_when_configured(self):
        self.source.content = {**CONFIG, "PROXY_USER_DN": "cn=svc", "PROXY_USER_PASSWORD": "pw"}
        self.source.save()
        report, connection = self.run_sync({})
        self.assertEqual(report.bind, "proxy")
        connection.simple_bind_s.assert_called_once_with("cn=svc", "pw")

    def test_dry_run(self):
        report, _ = self.run_sync({"xnovakova": ldap_attrs("a@stuba.sk")}, dry_run=True)
        self.assertEqual(report.summary["changed"], 1)
        self.assertFalse(NotificationContact.objects.exists())
        self.alena.refresh_from_db()
        self.assertEqual(self.alena.name, "")

    def test_only_the_given_users(self):
        report, _ = self.run_sync({}, users=User.objects.filter(pk=self.alena.pk))
        self.assertEqual([r.username for r in report.results], ["xnovakova"])

    def test_management_command(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with patch(
            "apps.core.services.ldap_directory.ldap_initialize",
            return_value=directory({"xnovakova": ldap_attrs("a@stuba.sk")}),
        ):
            call_command("sync_ldap_profiles", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("[dry-run] stuba.sk (bind=anonymous): users=2 changed=1", output)
        self.assertIn("xnovakova: name updated, surname updated, email created (primary)", output)
        self.assertNotIn("a@stuba.sk", output)


class LoginImportTests(TestCase):
    def setUp(self):
        self.source = AuthSource.objects.create(name="stuba.sk", driver=AuthSource.Driver.LDAP, content=CONFIG)

    def login(self, username, attrs):
        connection = directory({username: attrs})
        with patch("apps.core.auth.ldap_initialize", return_value=connection):
            basic = base64.b64encode(f"{username}:secret".encode()).decode()
            return BasicBackend().authenticate(None, basic=basic), connection

    def test_login_imports_the_address_through_the_shared_rule(self):
        user, connection = self.login("xnovakova", ldap_attrs("alena@stuba.sk"))
        self.assertEqual(NotificationService.resolve_recipient_email(user), "alena@stuba.sk")
        self.assertEqual(user.name, "Alena")
        self.assertNotIn("*", connection.searches[0])

    def test_login_survives_a_user_with_two_addresses(self):
        user = User.objects.create(username="xnovakova", auth_source=self.source)
        NotificationService.set_contact(user, EMAIL, "a@gmail.com")
        NotificationService.set_contact(user, EMAIL, "b@gmail.com", is_primary=False)

        logged_in, _ = self.login("xnovakova", ldap_attrs("alena@stuba.sk"))

        self.assertEqual(logged_in.pk, user.pk)
        self.assertEqual(NotificationService.resolve_recipient_email(user), "a@gmail.com")

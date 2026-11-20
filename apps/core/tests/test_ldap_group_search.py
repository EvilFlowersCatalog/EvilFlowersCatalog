"""#216: groups from group entries (`GROUP_SEARCH`) and the opt-in `SUPERADMIN_REVOKE`.

KU's directory (dr01.ku.sk) has no `memberOf` overlay: membership lives only on the
`groupOfNames` entry (`member: uid=…`). The directory is faked; what is pinned down here is
which searches run on which bind, that a failed group lookup never fails the login, and that
a source without `GROUP_SEARCH` behaves as before.
"""

import base64
import logging
from unittest.mock import patch

import ldap
from django.contrib.auth.models import Group
from django.test import TestCase

from apps.core.auth import BasicBackend
from apps.core.models import AuthSource, User
from apps.core.services.ldap_directory import sync_users
from apps.core.tests.test_tracing import recorded, request_span

PEOPLE = "ou=People,dc=ku,dc=sk"
GROUPS = "ou=Groups,dc=ku,dc=sk"
ADMINS = "cn=elvira_admins,ou=Groups,dc=ku,dc=sk"
LIBRARIANS = "cn=librarians,ou=Groups,dc=ku,dc=sk"

DIRECT = {
    "URI": "ldaps://dr01.ku.sk",
    "BIND": "uid={username}," + PEOPLE,
    "FILTER": "(uid={username})",
    "ROOT_DN": PEOPLE,
    "GROUP_ATTR": "memberOf",
    "GROUP_MAP": {"CN=Librarians,OU=Groups,DC=ku,DC=sk": "librarian"},
    "SUPERADMIN_GROUP": ADMINS,
    "USER_ATTR_MAP": {"name": "givenName"},
    "GROUP_SEARCH": {"BASE": GROUPS, "FILTER": "(&(objectClass=groupOfNames)(member={user_dn}))"},
}
PROXY = {**DIRECT, "PROXY_USER_DN": "uid=proxy,ou=Services,dc=ku,dc=sk", "PROXY_USER_PASSWORD": "proxy-secret"}


class FakeDirectory:
    """One fake LDAP connection for every `ldap_initialize` call; logs binds and searches in order.

    `entries`: username → (dn, attrs). `groups`: user DN → group DNs (groupOfNames `member`).
    `group_error`: raised by the group search.
    """

    def __init__(self, entries, groups=None, group_error=None, member_uid=None):
        self.entries = entries
        self.groups = groups or {}
        self.group_error = group_error
        self.member_uid = member_uid or {}
        self.log = []
        self._results = {}

    def set_option(self, *args):
        pass

    def simple_bind_s(self, dn, password):
        self.log.append(("bind", dn))

    def unbind(self):
        self.log.append(("unbind",))

    unbind_s = unbind

    def search(self, base, scope, filterstr, attributes):
        self.log.append(("search", base, filterstr, attributes))
        message_id = len(self.log)
        if base == GROUPS:
            if self.group_error:
                raise self.group_error
            matched = [dn for dn, members in self.groups.items() if any(m in filterstr for m in members)]
            matched += [
                dn for dn, uids in self.member_uid.items() if any(f"(memberUid={u})" in filterstr for u in uids)
            ]
            self._results[message_id] = [(dn, {}) for dn in matched] + [(None, ["ldap://elsewhere"])]
        else:
            username = filterstr[len("(uid=") : -1]
            entry = self.entries.get(username)
            self._results[message_id] = [entry] if entry else []
        return message_id

    def result(self, message_id, *args, **kwargs):
        return 101, self._results[message_id]

    def searches(self, base=None):
        return [e for e in self.log if e[0] == "search" and (base is None or e[1] == base)]


def entry(username, dn=None, member_of=()):
    attrs = {"givenName": [b"Elvira"]}
    if member_of:
        attrs["memberOf"] = [g.encode() for g in member_of]
    return dn or f"uid={username},{PEOPLE}", attrs


class GroupSearchLoginTests(TestCase):
    def setUp(self):
        Group.objects.create(name="librarian")

    def login(self, config, directory, username="elvira"):
        AuthSource.objects.update_or_create(
            name="ku.sk", defaults={"driver": AuthSource.Driver.LDAP, "content": config, "is_active": True}
        )
        with patch("apps.core.auth.ldap_initialize", return_value=directory):
            basic = base64.b64encode(f"{username}:secret".encode()).decode()
            return BasicBackend().authenticate(None, basic=basic)

    def test_member_of_the_admin_group_becomes_superuser(self):
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        user = self.login(DIRECT, directory)
        self.assertTrue(user.is_superuser)
        self.assertEqual(list(user.groups.all()), [])

    def test_group_map_matches_searched_dns_ignoring_case(self):
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={LIBRARIANS: [f"uid=elvira,{PEOPLE}"]})
        user = self.login(DIRECT, directory)
        self.assertEqual([g.name for g in user.groups.all()], ["librarian"])
        self.assertFalse(user.is_superuser)

    def test_searched_dns_match_ignoring_spacing(self):
        config = {**DIRECT, "GROUP_MAP": {"cn=librarians, ou=Groups, dc=ku, dc=sk": "librarian"}}
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={LIBRARIANS: [f"uid=elvira,{PEOPLE}"]})
        self.assertEqual([g.name for g in self.login(config, directory).groups.all()], ["librarian"])

    def test_admin_group_also_in_group_map_without_a_django_group_still_grants(self):
        # Before #216 the loop's `continue` on a missing Django group skipped the superadmin check too.
        config = {k: v for k, v in DIRECT.items() if k != "GROUP_SEARCH"}
        config["GROUP_MAP"] = {ADMINS: "no-such-group"}
        directory = FakeDirectory({"elvira": entry("elvira", member_of=[ADMINS])})
        self.assertTrue(self.login(config, directory).is_superuser)

    def test_user_outside_every_group_is_unchanged(self):
        directory = FakeDirectory({"reader": entry("reader")}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        user = self.login(DIRECT, directory, username="reader")
        self.assertFalse(user.is_superuser)
        self.assertEqual(list(user.groups.all()), [])

    def test_direct_mode_searches_on_the_user_bind_with_the_escaped_entry_dn(self):
        odd_dn = f"cn=Doe (ext)*,{PEOPLE}"
        directory = FakeDirectory({"elvira": entry("elvira", dn=odd_dn)}, groups={ADMINS: [r"Doe \28ext\29\2a"]})
        user = self.login(DIRECT, directory)

        (group_search,) = directory.searches(GROUPS)
        self.assertEqual(
            group_search[2], r"(&(objectClass=groupOfNames)(member=cn=Doe \28ext\29\2a,ou=People,dc=ku,dc=sk))"
        )
        self.assertEqual(group_search[3], ["1.1"])
        binds = [e for e in directory.log[: directory.log.index(group_search)] if e[0] == "bind"]
        self.assertEqual(binds, [("bind", f"uid=elvira,{PEOPLE}")])
        self.assertTrue(user.is_superuser)

    def test_proxy_mode_searches_on_the_proxy_bind(self):
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        user = self.login(PROXY, directory)

        (group_search,) = directory.searches(GROUPS)
        binds = [e[1] for e in directory.log[: directory.log.index(group_search)] if e[0] == "bind"]
        self.assertEqual(binds, [PROXY["PROXY_USER_DN"], f"uid=elvira,{PEOPLE}", PROXY["PROXY_USER_DN"]])
        self.assertTrue(user.is_superuser)

    def test_posix_group_by_username(self):
        config = {
            **DIRECT,
            "GROUP_SEARCH": {"BASE": GROUPS, "FILTER": "(&(objectClass=posixGroup)(memberUid={username}))"},
        }
        directory = FakeDirectory({"elvira": entry("elvira")}, member_uid={ADMINS: ["elvira"]})
        self.assertTrue(self.login(config, directory).is_superuser)

    def test_union_with_group_attr_values(self):
        config = {**DIRECT, "GROUP_MAP": {LIBRARIANS: "librarian"}}
        directory = FakeDirectory(
            {"elvira": entry("elvira", member_of=[LIBRARIANS])}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]}
        )
        user = self.login(config, directory)
        self.assertTrue(user.is_superuser)
        self.assertEqual([g.name for g in user.groups.all()], ["librarian"])

    def test_failed_group_search_keeps_the_login_and_the_attr_groups(self):
        config = {**DIRECT, "GROUP_MAP": {LIBRARIANS: "librarian"}}
        directory = FakeDirectory(
            {"elvira": entry("elvira", member_of=[LIBRARIANS])}, group_error=ldap.SERVER_DOWN({"desc": "down"})
        )
        with self.assertLogs(level=logging.WARNING) as logs:
            user = self.login(config, directory)
        self.assertIsNotNone(user)
        self.assertEqual([g.name for g in user.groups.all()], ["librarian"])
        self.assertIn("LDAP group search failed for elvira", "\n".join(logs.output))

    def test_unusable_group_search_config_keeps_the_login(self):
        for group_search in ({"BASE": GROUPS}, {"BASE": GROUPS, "FILTER": "(member={dn})"}, "ou=Groups"):
            with self.subTest(group_search=group_search):
                directory = FakeDirectory({"elvira": entry("elvira")})
                with self.assertLogs(level=logging.WARNING):
                    user = self.login({**DIRECT, "GROUP_SEARCH": group_search}, directory)
                self.assertIsNotNone(user)
                self.assertEqual(directory.searches(GROUPS), [])

    def test_source_without_group_map_or_superadmin_keys(self):
        config = {k: v for k, v in DIRECT.items() if k not in ("GROUP_MAP", "SUPERADMIN_GROUP")}
        directory = FakeDirectory(
            {"elvira": entry("elvira", member_of=[LIBRARIANS])}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]}
        )
        user = self.login(config, directory)
        self.assertFalse(user.is_superuser)
        self.assertEqual(list(user.groups.all()), [])

    def test_without_group_search_only_the_profile_search_runs(self):
        config = {k: v for k, v in DIRECT.items() if k != "GROUP_SEARCH"}
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        user = self.login(config, directory)
        self.assertEqual(len(directory.searches()), 1)
        self.assertFalse(user.is_superuser)


class SuperadminRevokeTests(TestCase):
    def login_as_admin(self, config, groups, group_error=None):
        source = AuthSource.objects.create(name="ku.sk", driver=AuthSource.Driver.LDAP, content=config)
        User.objects.create(username="elvira", is_superuser=True, auth_source=source)
        directory = FakeDirectory({"elvira": entry("elvira")}, groups=groups, group_error=group_error)
        with patch("apps.core.auth.ldap_initialize", return_value=directory):
            basic = base64.b64encode(b"elvira:secret").decode()
            return BasicBackend().authenticate(None, basic=basic)

    def test_off_by_default_a_hand_promoted_superuser_stays(self):
        self.assertTrue(self.login_as_admin(DIRECT, groups={}).is_superuser)

    def test_on_a_login_outside_the_admin_group_clears_it(self):
        self.assertFalse(self.login_as_admin({**DIRECT, "SUPERADMIN_REVOKE": True}, groups={}).is_superuser)

    def test_on_a_member_keeps_it(self):
        user = self.login_as_admin({**DIRECT, "SUPERADMIN_REVOKE": True}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        self.assertTrue(user.is_superuser)

    def test_on_a_failed_group_search_never_demotes(self):
        config = {**DIRECT, "SUPERADMIN_REVOKE": True}
        with self.assertLogs(level=logging.WARNING):
            user = self.login_as_admin(config, groups={}, group_error=ldap.TIMEOUT({"desc": "timeout"}))
        self.assertTrue(user.is_superuser)

    def test_on_without_a_superadmin_group_never_demotes(self):
        config = {k: v for k, v in DIRECT.items() if k != "SUPERADMIN_GROUP"}
        self.assertTrue(self.login_as_admin({**config, "SUPERADMIN_REVOKE": True}, groups={}).is_superuser)


class GroupSearchSpanTests(TestCase):
    def test_client_span_counts_groups_and_records_no_dn(self):
        AuthSource.objects.create(name="ku.sk", driver=AuthSource.Driver.LDAP, content=DIRECT)
        directory = FakeDirectory(
            {"elvira": entry("elvira")},
            groups={ADMINS: [f"uid=elvira,{PEOPLE}"], LIBRARIANS: [f"uid=elvira,{PEOPLE}"]},
        )
        with recorded() as exporter, patch("apps.core.auth.ldap_initialize", return_value=directory):
            with request_span():
                BasicBackend().authenticate(None, basic=base64.b64encode(b"elvira:secret").decode())

        (span,) = [
            s for s in exporter.get_finished_spans() if s.attributes.get("evilflowers.ldap.purpose") == "groups"
        ]
        self.assertEqual(span.name, "ldap search")
        self.assertEqual(span.attributes["evilflowers.ldap.groups"], 2)
        self.assertEqual(span.attributes["server.address"], "dr01.ku.sk")
        self.assertFalse([v for v in span.attributes.values() if "dc=ku" in str(v).lower()])


class SyncNeverSearchesGroupsTests(TestCase):
    def test_sync_ldap_profiles_ignores_group_search(self):
        source = AuthSource.objects.create(name="ku.sk", driver=AuthSource.Driver.LDAP, content=PROXY)
        user = User.objects.create(username="elvira", auth_source=source)
        directory = FakeDirectory({"elvira": entry("elvira")}, groups={ADMINS: [f"uid=elvira,{PEOPLE}"]})
        with patch("apps.core.services.ldap_directory.ldap_initialize", return_value=directory):
            sync_users(source, [user])
        self.assertEqual(directory.searches(GROUPS), [])
        user.refresh_from_db()
        self.assertFalse(user.is_superuser)

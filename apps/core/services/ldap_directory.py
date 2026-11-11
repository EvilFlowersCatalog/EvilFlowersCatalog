"""LDAP profile import shared by the login path and the background sync (IP-025).

Before IP-025 the e-mail address of an LDAP user was imported only inside
`BasicBackend._ldap`, i.e. when the user typed their password. A student who
had not logged in since `NOTIFICATION_CONTACT_MAP` was configured had no
contact, and every notification for them was logged `no_contact`. On the dev
deployment that was 11 of 17 `stuba.sk` users (checked 2026-11-09).

The sync binds *without* the user's password: with `PROXY_USER_DN` when the
auth source has a service account, anonymously otherwise. ldap.stuba.sk
answers an anonymous search for `(uid=…)` but filters `*` — it returns `mail`,
`givenName` and `sn` only when they are requested **by name**. So every search
here asks for an explicit attribute list built from the auth source config;
that is also what lets an operational `memberOf` come back on servers that
keep it out of `*`.

What the sync refreshes is deliberately narrower than a login: profile
attributes (`USER_ATTR_MAP`) and notification contacts
(`NOTIFICATION_CONTACT_MAP`). Group membership and the superuser flag stay a
login-time decision — a background job that can grant administrator rights
from a directory read is not something to run unattended.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

import ldap
import ldap.filter
from django.db import transaction

from apps.core.models import AuthSource, User
from evil_flowers_catalog import telemetry, tracing

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 60


def _server(config: dict) -> tuple:
    """(`server.address`, `server.port`) of the directory, for its CLIENT spans (IP-029)."""
    parsed = urlparse(config.get("URI", ""))
    port = parsed.port or (636 if parsed.scheme == "ldaps" else 389)
    return parsed.hostname, port


def ldap_bind(connection, config: dict, dn: Optional[str], password: Optional[str], *, mode: str) -> None:
    """`simple_bind_s` as a CLIENT span. `mode`: `user`, `proxy` or `anonymous`; the DN is never recorded.

    A refused password (`INVALID_CREDENTIALS`) is the reader's mistake, not a directory fault.
    """
    address, port = _server(config)
    with tracing.client_span(
        "ldap bind",
        system="ldap",
        server_address=address,
        server_port=port,
        attributes={"evilflowers.ldap.bind_mode": mode},
        expected=(ldap.INVALID_CREDENTIALS,),
    ):
        connection.simple_bind_s(dn or "", password or "")


def ldap_initialize(config: dict) -> "ldap.ldapobject.LDAPObject":
    uri = config["URI"]
    parsed = urlparse(uri)

    # Apply default port based on protocol if not specified in URI
    if not parsed.port:
        default_port = 636 if parsed.scheme == "ldaps" else 389
        uri = urlunparse(parsed._replace(netloc=f"{parsed.hostname}:{default_port}"))

    connection = ldap.initialize(uri=uri)
    connection.set_option(ldap.OPT_REFERRALS, 0)

    if parsed.scheme == "ldaps":
        connection.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
        ca_cert_file = config.get("CA_CERT_FILE")
        if ca_cert_file:
            connection.set_option(ldap.OPT_X_TLS_CACERTFILE, ca_cert_file)
        connection.set_option(ldap.OPT_X_TLS_NEWCTX, 0)

    return connection


def requested_attributes(config: dict, *, include_groups: bool = True) -> List[str]:
    """The attributes to ask for by name. Never `*` — see the module docstring."""
    names = set(config.get("USER_ATTR_MAP", {}).values())
    names.update((config.get("NOTIFICATION_CONTACT_MAP") or {}).values())
    if include_groups:
        names.add(config.get("GROUP_ATTR", "memberOf"))
    return sorted(names)


def first_value(attrs: dict, name: str) -> Optional[str]:
    values = attrs.get(name) or []
    if not values:
        return None
    value = values[0]
    value = value.decode() if isinstance(value, bytes) else str(value)
    return value.strip() or None


def search_profile(connection, config: dict, username: str, attributes: List[str]) -> Optional[dict]:
    """The directory entry for `username`, or None when the directory has none."""
    address, port = _server(config)
    with tracing.client_span("ldap search", system="ldap", server_address=address, server_port=port) as span:
        message_id = connection.search(
            config["ROOT_DN"],
            ldap.SCOPE_SUBTREE,
            config["FILTER"].format(username=ldap.filter.escape_filter_chars(username)),
            attributes,
        )
        # `result(msgid, all, timeout)` — the old login code passed 60 positionally as `all`, so it never timed out.
        _type, results = connection.result(message_id, timeout=SEARCH_TIMEOUT)
        # Referrals come back as (None, [urls]) — skip them.
        for dn, attrs in results or []:
            if dn:
                span.set_attribute("evilflowers.ldap.found", True)
                return attrs
        span.set_attribute("evilflowers.ldap.found", False)
        return None


@dataclass
class ContactChange:
    contact_type: str
    action: str  # created | updated | adopted | promoted | unchanged
    is_primary: bool


@dataclass
class ProfileChange:
    username: str
    found: bool = True
    profile: Dict[str, str] = field(default_factory=dict)  # changed model fields → new value
    contacts: List[ContactChange] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.profile) or any(c.action != "unchanged" for c in self.contacts)


def apply_profile(user: User, attrs: dict, config: dict, *, dry_run: bool = False) -> ProfileChange:
    """Copy `USER_ATTR_MAP` attributes and `NOTIFICATION_CONTACT_MAP` contacts onto `user`.

    Does not save `user` — the login path saves it once together with the
    group changes; `sync_users` saves it itself. Contacts are written here
    (unless `dry_run`), because the user must already exist for them.
    """
    from apps.notifications.services import NotificationService

    change = ProfileChange(username=user.username)

    for model_property, ldap_property in config.get("USER_ATTR_MAP", {}).items():
        value = first_value(attrs, ldap_property)
        if value is not None and getattr(user, model_property, None) != value:
            change.profile[model_property] = value
            if not dry_run:
                setattr(user, model_property, value)

    for contact_type, ldap_property in (config.get("NOTIFICATION_CONTACT_MAP") or {}).items():
        value = first_value(attrs, ldap_property)
        if not value:
            continue
        if contact_type == "email":
            value = value.lower()
        change.contacts.append(NotificationService.sync_directory_contact(user, contact_type, value, dry_run=dry_run))

    return change


@dataclass
class SyncReport:
    auth_source: str
    bind: str  # proxy | anonymous
    results: List[ProfileChange] = field(default_factory=list)

    @property
    def summary(self) -> Dict[str, int]:
        return {
            "users": len(self.results),
            "changed": sum(1 for r in self.results if r.found and not r.error and r.changed),
            "unchanged": sum(1 for r in self.results if r.found and not r.error and not r.changed),
            "not_found": sum(1 for r in self.results if not r.found),
            "errors": sum(1 for r in self.results if r.error),
        }


class DirectorySyncError(Exception):
    pass


def _bind_for_sync(config: dict):
    connection = ldap_initialize(config)
    if config.get("PROXY_USER_DN"):
        ldap_bind(connection, config, config["PROXY_USER_DN"], config.get("PROXY_USER_PASSWORD"), mode="proxy")
        return connection, "proxy"
    ldap_bind(connection, config, "", "", mode="anonymous")
    return connection, "anonymous"


def _sync_user(connection, config: dict, user: User, attributes: List[str], *, dry_run: bool) -> ProfileChange:
    try:
        attrs = search_profile(connection, config, user.username, attributes)
    except ldap.LDAPError as error:
        logger.warning("LDAP sync: search for %s failed: %s", user.username, error)
        return ProfileChange(username=user.username, error=str(error))

    if attrs is None:
        return ProfileChange(username=user.username, found=False)

    with transaction.atomic():
        change = apply_profile(user, attrs, config, dry_run=dry_run)
        if change.profile and not dry_run:
            user.save(update_fields=[*change.profile.keys(), "updated_at"])
    return change


def sync_users(
    auth_source: AuthSource,
    users: Optional[Iterable[User]] = None,
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Refresh profile attributes and notification contacts of the users of one LDAP source.

    `users` defaults to every active user of `auth_source`. One bind serves
    the whole run. A user the directory no longer knows is reported
    `found=False` and left alone — deactivating accounts is not this job's
    decision.
    """
    if auth_source.driver != AuthSource.Driver.LDAP:
        raise DirectorySyncError(f"Auth source {auth_source.name} is not an LDAP source")

    config = auth_source.content or {}
    if not config.get("NOTIFICATION_CONTACT_MAP") and not config.get("USER_ATTR_MAP"):
        raise DirectorySyncError(
            f"Auth source {auth_source.name} maps no attributes (USER_ATTR_MAP / NOTIFICATION_CONTACT_MAP)"
        )

    if users is None:
        users = User.objects.filter(auth_source=auth_source, is_active=True).order_by("username")

    try:
        connection, bind = _bind_for_sync(config)
    except ldap.LDAPError as error:
        raise DirectorySyncError(f"Cannot bind to {config.get('URI')}: {error}") from error

    report = SyncReport(auth_source=auth_source.name, bind=bind)
    attributes = requested_attributes(config, include_groups=False)

    try:
        # From `manage.py sync_ldap_profiles` one trace per user, linked to the command: a run over
        # thousands of users would be one trace too large to open. Inside a request (the MCP tool, a
        # handful of users) a child span per user keeps the work in the request's trace (IP-029 Q2).
        per_user = tracing.root_span if telemetry.current_role() == "command" else tracing.span
        for user in users:
            with per_user("ldap.sync.user", {"evilflowers.auth_source.id": auth_source.pk}):
                report.results.append(_sync_user(connection, config, user, attributes, dry_run=dry_run))
    finally:
        try:
            connection.unbind_s()
        except ldap.LDAPError:
            pass

    logger.info(
        "LDAP sync auth_source=%s bind=%s dry_run=%s %s",
        auth_source.name,
        bind,
        dry_run,
        " ".join(f"{k}={v}" for k, v in report.summary.items()),
    )
    return report


__all__ = [
    "ContactChange",
    "DirectorySyncError",
    "ProfileChange",
    "SyncReport",
    "apply_profile",
    "first_value",
    "ldap_initialize",
    "requested_attributes",
    "search_profile",
    "sync_users",
]

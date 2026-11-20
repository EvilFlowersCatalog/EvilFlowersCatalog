import base64
import binascii
import logging
import uuid
from datetime import timedelta
from functools import cache
from http import HTTPStatus
from typing import Optional, TypedDict, Dict, List, Tuple
from urllib.parse import urlparse

import ldap
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeyFlexible, import_key
from joserfc.jwt import JWTClaimsRegistry
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import ApiKey, User, AuthSource, UserCatalog
from apps.core.services.ldap_directory import (
    GroupSearchConfigError,
    apply_profile,
    ldap_bind,
    ldap_initialize,
    normalize_dn,
    requested_attributes,
    search_groups,
    search_profile_entry,
)
from evil_flowers_catalog import tracing


@cache
def _jwt_key() -> KeyFlexible:
    """The signing key, imported once per process (#125).

    `SECURED_VIEW_JWK` is the RSA private key; importing it was a third of the
    sampled CPU when it ran on every bearer-authenticated request.
    """
    return import_key(settings.SECURED_VIEW_JWK)


@cache
def _jwt_verify_key() -> KeyFlexible:
    """The public half of `_jwt_key`, which is all that verification needs."""
    return import_key(_jwt_key().as_dict(private=False))


@receiver(setting_changed)
def _clear_jwt_keys(setting, **kwargs):
    if setting == "SECURED_VIEW_JWK":
        _jwt_key.cache_clear()
        _jwt_verify_key.cache_clear()


#: `ApiKey.last_seen_at` is written at most this often per key (#125); it was a full-row UPDATE per request.
API_KEY_LAST_SEEN_RESOLUTION = timedelta(minutes=1)


class JWTFactory:
    def __init__(self, user_id: str):
        self._user_id = str(user_id)

    def _generate(self, additional_payload: dict) -> str:
        base_payload = {
            "iss": settings.INSTANCE_NAME,
            "sub": self._user_id,
            "iat": timezone.now(),
        }

        return jwt.encode(
            header={"alg": settings.SECURED_VIEW_JWT_ALGORITHM},
            claims={**base_payload, **additional_payload},
            key=_jwt_key(),
        )

    def refresh(self) -> tuple:
        jti = str(uuid.uuid4())
        return jti, self._generate(
            {
                "type": "refresh",
                "jti": jti,
                "exp": timezone.now() + settings.SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION,
            }
        )

    def access(self) -> str:
        return self._generate(
            {
                "type": "access",
                "exp": timezone.now() + settings.SECURED_VIEW_JWT_ACCESS_TOKEN_EXPIRATION,
            }
        )

    def api_key(self, jti: str) -> str:
        return self._generate(
            {
                "type": "api_key",
                "jti": jti,
            }
        )

    def scoped(self, scope: str) -> str:
        """Issue a time-limited JWT scoped to a specific action.

        Used by the notification engine to generate download links
        that authenticate the user for a specific resource. A unique
        `jti` claim is always emitted so consumers can implement
        single-use semantics by writing the `jti` to a deny-list once
        a state-mutating action has been performed (see IP-011 Phase 1
        — the reservation_claim flow consumes the token after a
        successful `ReservationService.claim`).
        """
        return self._generate(
            {
                "type": "scoped",
                "scope": scope,
                "jti": str(uuid.uuid4()),
                "exp": timezone.now()
                + timedelta(hours=getattr(settings, "EVILFLOWERS_NOTIFICATION_SCOPED_TOKEN_TTL_HOURS", 72)),
            }
        )

    @classmethod
    def decode(cls, token: str):
        decoded = jwt.decode(
            token,
            _jwt_verify_key(),
            algorithms=[settings.SECURED_VIEW_JWT_ALGORITHM],
        )
        JWTClaimsRegistry().validate(decoded.claims)
        return decoded.claims


class BearerBackend(ModelBackend):
    def authenticate(self, request, bearer=None, **kwargs):
        try:
            claims = JWTFactory.decode(bearer)
        except JoseError as e:
            raise ProblemDetailException(_("Invalid token."), status=HTTPStatus.UNAUTHORIZED, previous=e)

        # `access` | `api_key` | `scoped`: which kind of bearer token, for the server span (IP-029).
        setattr(request, "auth_token_type", str(claims.get("type")))
        tracing.set_attributes({tracing.AUTH_TOKEN_TYPE: claims.get("type")})

        if claims["type"] == "api_key":
            try:
                api_key = ApiKey.objects.get(pk=claims["jti"], is_active=True)
            except (ApiKey.DoesNotExist, ValidationError):
                raise ProblemDetailException(_("Invalid api key."), status=HTTPStatus.UNAUTHORIZED)

            now = timezone.now()
            if api_key.last_seen_at is None or now - api_key.last_seen_at >= API_KEY_LAST_SEEN_RESOLUTION:
                ApiKey.objects.filter(pk=api_key.pk).update(last_seen_at=now)
                api_key.last_seen_at = now
            setattr(request, "api_key", api_key)
            user = api_key.user
        elif claims["type"] == "access":
            try:
                user = User.objects.get(pk=claims["sub"])
            except User.DoesNotExist:
                raise ProblemDetailException(_("Inactive user."), status=HTTPStatus.FORBIDDEN)
        elif claims["type"] == "scoped":
            try:
                user = User.objects.get(pk=claims["sub"])
            except User.DoesNotExist:
                raise ProblemDetailException(_("Inactive user."), status=HTTPStatus.FORBIDDEN)
        else:
            raise ProblemDetailException(_("Invalid token"), status=HTTPStatus.UNAUTHORIZED)

        if not self.user_can_authenticate(user):
            raise ProblemDetailException(_("Inactive user."), status=HTTPStatus.FORBIDDEN)

        return user


class BasicBackend(ModelBackend):
    class LdapConfig(TypedDict):
        URI: str
        CA_CERT_FILE: Optional[str]
        ROOT_DN: str
        BIND: str
        USER_ATTR_MAP: Dict[str, str]
        GROUP_MAP: Dict[str, str]
        GROUP_ATTR: str
        FILTER: str
        SUPERADMIN_GROUP: Optional[str]
        CATALOGS: Optional[Dict[str, str]]
        PROXY_USER_DN: Optional[str]
        PROXY_USER_PASSWORD: Optional[str]
        NOTIFICATION_CONTACT_MAP: Optional[Dict[str, str]]
        # #216: {"BASE": ..., "FILTER": "...{user_dn}..." or "...{username}..."}; groups from group entries.
        GROUP_SEARCH: Optional[Dict[str, str]]
        # #216: true = a login outside SUPERADMIN_GROUP clears is_superuser (default: never revoked).
        SUPERADMIN_REVOKE: Optional[bool]

    @staticmethod
    def _ldap_initialize(config: "BasicBackend.LdapConfig") -> ldap.ldapobject.LDAPObject:
        return ldap_initialize(config)

    def _ldap(self, username: str, password: str, auth_source: AuthSource) -> Optional[User]:
        config: BasicBackend.LdapConfig = auth_source.content
        connection = self._ldap_initialize(config)

        # Proxy user authentication mode
        if config.get("PROXY_USER_DN", False):
            try:
                # Bind with service account
                ldap_bind(connection, config, config["PROXY_USER_DN"], config["PROXY_USER_PASSWORD"], mode="proxy")

                # Search for user to verify existence and get DN
                with tracing.client_span(
                    "ldap search",
                    system="ldap",
                    server_address=urlparse(config["URI"]).hostname,
                    attributes={"evilflowers.ldap.purpose": "resolve_dn"},
                ):
                    user_search = connection.search(
                        f"{config['ROOT_DN']}",
                        ldap.SCOPE_SUBTREE,
                        config["FILTER"].format(username=username),
                        ["dn"],
                    )

                    user_type, user_results = connection.result(user_search, 60)

                if not user_results:
                    logging.warning(f"User {username} not found in LDAP directory")
                    return None

                user_dn = user_results[0][0]  # Get the user's DN

                # Unbind proxy connection and test user credentials
                connection.unbind()
                connection = self._ldap_initialize(config)
                ldap_bind(connection, config, user_dn, password, mode="user")

                # Rebind with service account for attribute retrieval
                connection.unbind()
                connection = self._ldap_initialize(config)
                ldap_bind(connection, config, config["PROXY_USER_DN"], config["PROXY_USER_PASSWORD"], mode="proxy")

            except ldap.LDAPError as e:
                logging.warning(
                    f"Unable to authenticate with proxy user (id={auth_source.pk}, name={auth_source.name}): {e}"
                )
                return None
        else:
            # Direct authentication mode (existing behavior)
            user_dn = None
            try:
                ldap_bind(connection, config, config["BIND"].format(username=username), password, mode="user")
            except ldap.LDAPError as e:
                logging.warning(
                    f"Unable to bind with external service (id={auth_source.pk}, name={auth_source.name}): {e}"
                )
                return None

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            user = User(username=username, auth_source=auth_source)
            user.set_unusable_password()
            user.save()

        # Attributes are requested by name: ldap.stuba.sk filters `*` for
        # anonymous binds, and servers keep an operational `memberOf` out of it.
        entry = search_profile_entry(connection, config, username, requested_attributes(config))

        if entry is not None:
            entry_dn, attrs = entry
            # Profile attributes and notification contacts — shared with the
            # background sync (`sync_ldap_profiles`, IP-025).
            apply_profile(user, attrs, config)

            attr_groups = [value.decode() for value in attrs.get(config.get("GROUP_ATTR", "memberOf"), [])]
            searched_groups, groups_complete = [], True
            if config.get("GROUP_SEARCH"):
                # Proxy mode resolved the DN already; direct mode has the profile entry's DN
                # (BIND may be a UPN like "{username}", not a DN).
                searched_groups, groups_complete = self._search_groups(
                    connection, config, entry_dn or user_dn, username, auth_source
                )
            self._apply_groups(user, config, attr_groups, searched_groups, groups_complete)
        else:
            logging.warning(
                f"Could not find user profile for {username} in auth source {auth_source.name}"
                f" (id={auth_source.pk}, name={auth_source.name})"
            )
            return None

        try:
            connection.unbind()
        except ldap.LDAPError:
            pass  # a connection a failed group search left dead must not fail the login (#216)
        user.save()

        for catalog_id, mode in config.get("CATALOGS", {}).items():
            if not UserCatalog.objects.filter(catalog_id=catalog_id, user=user).exists():
                UserCatalog.objects.create(catalog_id=catalog_id, user=user, mode=mode)

        return user

    @staticmethod
    def _search_groups(
        connection, config: "BasicBackend.LdapConfig", user_dn: Optional[str], username: str, auth_source: AuthSource
    ) -> Tuple[List[str], bool]:
        """`GROUP_SEARCH` (#216): `(group DNs, lookup succeeded)`. A failed lookup never fails the login."""
        if not user_dn:
            logging.warning(f"LDAP group search skipped for {username}: no user DN (auth source {auth_source.name})")
            return [], False
        try:
            return search_groups(connection, config, user_dn, username), True
        except (ldap.LDAPError, GroupSearchConfigError) as e:
            logging.warning(
                f"LDAP group search failed for {username} (id={auth_source.pk}, name={auth_source.name}): {e}"
            )
            return [], False

    @staticmethod
    def _apply_groups(
        user: User,
        config: "BasicBackend.LdapConfig",
        attr_groups: List[str],
        searched_groups: List[str],
        groups_complete: bool,
    ) -> None:
        """`GROUP_MAP` and `SUPERADMIN_GROUP` over the user's directory groups.

        `GROUP_ATTR` values match exactly, as they always have. `GROUP_SEARCH` results are DNs the
        server spells its own way, so they match ignoring case and spacing (`normalize_dn`).
        `SUPERADMIN_REVOKE` clears `is_superuser` when the groups do not contain
        `SUPERADMIN_GROUP` — only when that group is configured and the group lookup succeeded,
        so a directory hiccup never demotes an administrator.
        """
        group_map = config.get("GROUP_MAP") or {}
        superadmin_group = config.get("SUPERADMIN_GROUP")

        group_names = [group_map[group] for group in attr_groups if group in group_map]
        is_admin = superadmin_group is not None and superadmin_group in attr_groups
        if searched_groups:
            searched = {normalize_dn(dn) for dn in searched_groups}
            group_names += [value for key, value in group_map.items() if normalize_dn(key) in searched]
            is_admin = is_admin or (bool(superadmin_group) and normalize_dn(superadmin_group) in searched)

        user.groups.clear()
        for name in dict.fromkeys(group_names):
            try:
                user.groups.add(Group.objects.get(name=name))
            except Group.DoesNotExist:
                continue

        if is_admin:
            user.is_superuser = True
        elif config.get("SUPERADMIN_REVOKE") and superadmin_group and groups_complete:
            user.is_superuser = False

    def _database(self, request, username: str, password: str) -> Optional[User]:
        return super().authenticate(request, username=username, password=password)

    def authenticate(self, request, basic=None, **kwargs):
        try:
            bits = base64.b64decode(basic, validate=True).decode().split(":")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ProblemDetailException(
                _("Invalid credentials"),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    (
                        "WWW-Authenticate",
                        f"Basic realm={slugify(settings.INSTANCE_NAME)},"
                        f' Bearer realm="{slugify(settings.INSTANCE_NAME)}"',
                    ),
                ),
                previous=exc,
            )
        if len(bits) < 2:
            raise ProblemDetailException(
                _("Invalid credentials"),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    (
                        "WWW-Authenticate",
                        f"Basic realm={slugify(settings.INSTANCE_NAME)},"
                        f' Bearer realm="{slugify(settings.INSTANCE_NAME)}"',
                    ),
                ),
            )
        username = bits[0].lower()
        password = ":".join(bits[1:])
        user = None

        for auth_source in AuthSource.objects.filter(is_active=True):
            # One step per source tried, so a slow directory shows as its own bar (IP-029).
            with tracing.span(
                "auth.source",
                {"evilflowers.auth_source.id": auth_source.pk, "evilflowers.auth_source.driver": auth_source.driver},
            ) as span:
                if auth_source.driver == AuthSource.Driver.DATABASE:
                    user = self._database(request, username, password)
                elif auth_source.driver == AuthSource.Driver.LDAP:
                    user = self._ldap(username, password, auth_source)
                span.set_attribute(tracing.AUTH_OUTCOME, "accepted" if user else "rejected")

            if user:
                break

        if not user:
            raise ProblemDetailException(
                _("Invalid credentials"),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    (
                        "WWW-Authenticate",
                        f'Bearer realm="{slugify(settings.INSTANCE_NAME)}"',
                    ),
                ),
            )

        user.last_login = timezone.now()
        user.save()

        return user


__all__ = ["BasicBackend", "BearerBackend", "JWTFactory"]

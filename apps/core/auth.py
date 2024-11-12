import base64
import logging
import uuid
from http import HTTPStatus
from typing import Optional, TypedDict, Dict

import ldap
from authlib.jose import JsonWebToken, jwt
from authlib.jose.errors import JoseError
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import ApiKey, User, AuthSource, UserCatalog


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
            payload={**base_payload, **additional_payload},
            key=settings.SECURED_VIEW_JWK,
        ).decode()

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

    @classmethod
    def decode(cls, token: str):
        claims = JsonWebToken(settings.SECURED_VIEW_JWT_ALGORITHM).decode(token, settings.SECURED_VIEW_JWK)
        claims.validate()
        return claims


class BearerBackend(ModelBackend):
    def authenticate(self, request, bearer=None, **kwargs):
        try:
            claims = JWTFactory.decode(bearer)
        except JoseError as e:
            raise ProblemDetailException(_("Invalid token."), status=HTTPStatus.UNAUTHORIZED, previous=e)

        if claims["type"] == "api_key":
            try:
                api_key = ApiKey.objects.get(pk=claims["jti"], is_active=True)
            except (ApiKey.DoesNotExist, ValidationError):
                raise ProblemDetailException(_("Invalid api key."), status=HTTPStatus.UNAUTHORIZED)

            api_key.last_seen_at = timezone.now()
            api_key.save()
            setattr(request, "api_key", api_key)
            user = api_key.user
        elif claims["type"] == "access":
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
        ROOT_DN: str
        BIND: str
        USER_ATTR_MAP: Dict[str, str]
        GROUP_MAP: Dict[str, str]
        FILTER: str
        SUPERADMIN_GROUP: Optional[str]
        CATALOGS: Optional[Dict[str, str]]

    def _ldap(self, username: str, password: str, auth_source: AuthSource) -> Optional[User]:
        config: BasicBackend.LdapConfig = auth_source.content
        connection = ldap.initialize(uri=config["URI"])
        connection.set_option(ldap.OPT_REFERRALS, 0)

        try:
            connection.simple_bind_s(config["BIND"].format(username=username), password)
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

        result = connection.search(
            f"{config['ROOT_DN']}",
            ldap.SCOPE_SUBTREE,
            config["FILTER"].format(username=username),
            ["*"],
        )

        user_type, profiles = connection.result(result, 60)

        if profiles:
            name, attrs = profiles[0]

            # LDAP properties
            for model_property, ldap_property in config["USER_ATTR_MAP"].items():
                setattr(user, model_property, attrs[ldap_property][0].decode())

            # LDAP groups
            user.groups.clear()
            for ldap_group in attrs.get("memberOf", []):
                if ldap_group.decode() in config["GROUP_MAP"]:
                    try:
                        group = Group.objects.get(name=config["GROUP_MAP"][ldap_group.decode()])
                    except Group.DoesNotExist:
                        continue
                    user.groups.add(group)
                if ldap_group.decode() == config["SUPERADMIN_GROUP"]:
                    user.is_superuser = True
        else:
            logging.warning(
                f"Could not find user profile for {username} in auth source {auth_source.name}"
                f" (id={auth_source.pk}, name={auth_source.name})"
            )
            return None

        connection.unbind()
        user.save()

        for catalog_id, mode in config.get("CATALOGS", {}).items():
            if not UserCatalog.objects.filter(catalog_id=catalog_id, user=user).exists():
                UserCatalog.objects.create(catalog_id=catalog_id, user=user, mode=mode)

        return user

    def _database(self, request, username: str, password: str) -> Optional[User]:
        return super().authenticate(request, username=username, password=password)

    def authenticate(self, request, basic=None, **kwargs):
        bits = base64.b64decode(basic).decode().split(":")
        username = bits[0].lower()
        password = ":".join(bits[1:])
        user = None

        for auth_source in AuthSource.objects.filter(is_active=True):
            if auth_source.driver == AuthSource.Driver.DATABASE:
                user = self._database(request, username, password)
            elif auth_source.driver == AuthSource.Driver.LDAP:
                user = self._ldap(username, password, auth_source)

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

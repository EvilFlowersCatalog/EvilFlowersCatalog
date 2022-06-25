import base64
import uuid
from http import HTTPStatus

from authlib.jose import JsonWebToken, jwt
from authlib.jose.errors import JoseError
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import ApiKey, User, AuthSource


class JWTFactory:
    def __init__(self, user_id: str):
        self._user_id = str(user_id)

    def _generate(self, additional_payload: dict) -> str:
        base_payload = {
            'iss': settings.BASE_URL,
            'sub': self._user_id,
            'iat': timezone.now(),
        }

        return jwt.encode(
            header={
                'alg': settings.SECURED_VIEW_JWT_ALGORITHM
            },
            payload={**base_payload, **additional_payload},
            key=settings.SECURED_VIEW_JWK
        ).decode()

    def refresh(self) -> tuple:
        jti = str(uuid.uuid4())
        return jti, self._generate({
            'type': 'refresh',
            'jti': jti,
            'exp': timezone.now() + settings.SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION,
        })

    def access(self) -> str:
        return self._generate({
            'type': 'access',
            'exp': timezone.now() + settings.SECURED_VIEW_JWT_ACCESS_TOKEN_EXPIRATION,
        })

    def api_key(self, jti: str) -> str:
        return self._generate({
            'type': 'api_key',
            'jti': jti,
        })

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
            raise ProblemDetailException(request, _('Invalid token.'), status=HTTPStatus.UNAUTHORIZED, previous=e)

        if claims['type'] == 'api_key':
            try:
                api_key = ApiKey.objects.get(pk=claims['jti'], is_active=True)
            except (ApiKey.DoesNotExist, ValidationError):
                raise ProblemDetailException(request, _('Invalid api key.'), status=HTTPStatus.UNAUTHORIZED)

            api_key.last_seen_at = timezone.now()
            api_key.save()
            setattr(request, 'api_key', api_key)
            user = api_key.user
        elif claims['type'] == 'access':
            try:
                user = User.objects.get(pk=claims['sub'])
            except User.DoesNotExist:
                raise ProblemDetailException(request, _('Inactive user.'), status=HTTPStatus.FORBIDDEN)
        else:
            raise ProblemDetailException(request, _('Invalid token'), status=HTTPStatus.UNAUTHORIZED)

        if not self.user_can_authenticate(user):
            raise ProblemDetailException(request, _('Inactive user.'), status=HTTPStatus.FORBIDDEN)

        return user


class BasicBackend(ModelBackend):
    def authenticate(self, request, basic=None, **kwargs):
        auth_params = base64.b64decode(basic).decode().split(':')

        # TODO: use auth sources

        user = super().authenticate(request, username=auth_params[0], password=auth_params[1])
        if not user:
            raise ProblemDetailException(
                request,
                _('Invalid credentials'),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    ('WWW-Authenticate', f'Bearer realm="{slugify(settings.INSTANCE_NAME)}"'),
                )
            )
        return user


__all__ = [
    'BasicBackend',
    'BearerBackend',
    'JWTFactory'
]

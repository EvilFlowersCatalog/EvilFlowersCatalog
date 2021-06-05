import base64
from http import HTTPStatus

from django.contrib.auth.backends import UserModel, ModelBackend
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.api.errors import ProblemDetailException
from apps.core.models import ApiKey


class BearerBackend(ModelBackend):
    def authenticate(self, request, bearer=None):
        try:
            api_key = ApiKey.objects.get(pk=bearer, is_active=True)
        except (ApiKey.DoesNotExist, ValidationError):
            raise ProblemDetailException(request, _('Invalid api key.'), status=HTTPStatus.UNAUTHORIZED)

        setattr(request, 'api_key', api_key)
        return api_key.user


class BasicBackend(ModelBackend):
    def authenticate(self, request, basic=None, **kwargs):
        auth_params = base64.b64decode(basic).decode().split(':')
        user = super().authenticate(request, username=auth_params[0], password=auth_params[1])
        if not user:
            raise ProblemDetailException(
                request,
                _('Invalid credentials'),
                status=HTTPStatus.UNAUTHORIZED,
                extra_headers=(
                    ('WWW-Authenticate', 'Bearer realm="ewil-flowers-catalog'),  # TODO: application name
                )
            )
        return user


__all__ = [
    'BasicBackend',
    'BearerBackend'
]

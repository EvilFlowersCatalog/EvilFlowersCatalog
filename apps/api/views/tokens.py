from http import HTTPStatus

from authlib.jose import JoseError
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.utils.translation import gettext as _
from django.views import View
from redis.client import Redis

from apps.api.forms.tokens import AccessTokenForm, RefreshTokenForm
from apps.core.auth import JWTFactory
from apps.core.errors import ValidationException, UnauthorizedException, ProblemDetailException
from apps.api.response import SingleResponse
from apps.view.base import SecuredView


class AccessTokenManagement(SecuredView):
    def post(self, request):
        form = AccessTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        backend = ModelBackend()
        user = backend.authenticate(
            request,
            username=form.cleaned_data['username'],
            password=form.cleaned_data['password']
        )

        if not user:
            raise UnauthorizedException(request)

        access_token = JWTFactory(user.pk).access()
        jti, refresh_token = JWTFactory(user.pk).refresh()

        redis = Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DATABASE
        )

        redis.set(f"refresh_token:{jti}", jti)
        redis.expire(f"refresh_token:{jti}", settings.SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION)

        return SingleResponse(request, {
            'access_token': access_token,
            'refresh_token': refresh_token
        }, status=HTTPStatus.OK)


class RefreshTokenManagement(View):
    def post(self, request):
        form = RefreshTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        try:
            claims = JWTFactory.decode(form.cleaned_data['refresh'])
        except JoseError as e:
            raise ProblemDetailException(request, _('Invalid token.'), status=HTTPStatus.UNAUTHORIZED, previous=e)

        redis = Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DATABASE
        )

        if not redis.exists(f"refresh_token:{claims['jti']}"):
            raise UnauthorizedException(request)

        access_token = JWTFactory(claims['sub']).access()

        return SingleResponse(request, {
            'access_token': access_token
        }, status=HTTPStatus.OK)

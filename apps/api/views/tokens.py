import base64
from http import HTTPStatus

from authlib.jose import JoseError
from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _
from django.views import View

from apps import openapi
from apps.api.forms.tokens import AccessTokenForm, RefreshTokenForm
from apps.api.serializers.tokens import TokenSerializer
from apps.core.auth import JWTFactory, BasicBackend
from apps.core.errors import (
    ValidationException,
    UnauthorizedException,
)
from apps.api.response import SingleResponse
from apps.core.views import SecuredView


class AccessTokenManagement(SecuredView):
    @openapi.metadata(
        description="Generate a new access token and refresh token pair using username and password authentication. Returns both an access token for immediate API access and a refresh token for obtaining new access tokens. Requires valid user credentials and creates a cached refresh token session.",
        tags=["Tokens"],
        summary="Create access token",
    )
    def post(self, request):
        form = AccessTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        backend = BasicBackend()
        user = backend.authenticate(
            request,
            basic=base64.b64encode(
                f"{form.cleaned_data['username']}:{form.cleaned_data['password']}".encode()
            ).decode(),
        )

        if not user:
            raise UnauthorizedException()

        access_token = JWTFactory(user.pk).access()
        jti, refresh_token = JWTFactory(user.pk).refresh()

        cache.set(f"refresh_token:{jti}", jti, settings.SECURED_VIEW_JWT_REFRESH_TOKEN_EXPIRATION.total_seconds())

        return SingleResponse(
            request,
            data=TokenSerializer.Access(access_token=access_token, refresh_token=refresh_token, user=user),
        )


class RefreshTokenManagement(View):
    @openapi.metadata(
        description="Generate a new access token using a valid refresh token. Validates the refresh token against the cache and returns a new access token for continued API access. The refresh token must be previously issued and not expired.",
        tags=["Tokens"],
        summary="Refresh access token",
    )
    def post(self, request):
        form = RefreshTokenForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        try:
            claims = JWTFactory.decode(form.cleaned_data["refresh"])
        except JoseError as e:
            raise UnauthorizedException(_("Invalid token."), previous=e)

        if not cache.get(f"refresh_token:{claims['jti']}"):
            raise UnauthorizedException()

        return SingleResponse(
            request,
            data=TokenSerializer.Refresh(access_token=JWTFactory(claims["sub"]).access()),
            status=HTTPStatus.OK,
        )

from http import HTTPStatus

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.text import slugify
from django.views import View

from apps.opds2.schema.opds import AuthenticationDocument, AuthenticationFlow
from apps.opds2.schema.rwpm import Link

AUTHENTICATION_DOCUMENT_TYPE = "application/opds-authentication+json"
REL_AUTH_DOCUMENT = "http://opds-spec.org/auth/document"


def authentication_document(request) -> AuthenticationDocument:
    """The catalog's Authentication for OPDS 1.0 document."""
    base_url = f"{request.scheme}://{request.get_host()}"

    flows = [
        AuthenticationFlow(
            type="http://opds-spec.org/auth/basic",
            labels={"login": "Username or Email", "password": "Password"},
        ),
        # Not a registered flow type: readers that don't know it skip it
        # (Thorium picks the first type it supports, Basic above). Kept for
        # API clients that sign in through the JSON login endpoint.
        AuthenticationFlow(
            type="http://opds-spec.org/auth/bearer",
            links=[
                Link(
                    href=f"{base_url}{reverse('api:login')}",
                    type="application/json",
                    rel="authenticate",
                ),
            ],
        ),
    ]

    return AuthenticationDocument(
        id=f"{base_url}{reverse('opds2:auth')}",
        title="Evil Flowers Catalog",
        description="Sign in to borrow publications",
        authentication=flows,
    )


def authentication_required(request) -> JsonResponse:
    """`401` whose body is the Authentication Document.

    Authentication for OPDS 1.0 §2.1: a catalog answers an unauthenticated
    (or wrongly authenticated) request with the document itself, so the
    reader can show its sign-in dialog. Thorium reads a `problem+json` body
    as a plain error and never offers to sign in, so OPDS endpoints must not
    answer a 401 with one.
    """
    document = authentication_document(request)
    response = JsonResponse(
        document.model_dump(exclude_none=True, by_alias=True),
        status=HTTPStatus.UNAUTHORIZED,
        content_type=AUTHENTICATION_DOCUMENT_TYPE,
    )
    realm = slugify(settings.INSTANCE_NAME)
    response["WWW-Authenticate"] = f'Basic realm="{realm}"'
    response["Link"] = f'<{document.id}>; rel="{REL_AUTH_DOCUMENT}"; type="{AUTHENTICATION_DOCUMENT_TYPE}"'
    return response


class AuthenticationDocumentView(View):
    def get(self, request):
        return JsonResponse(
            authentication_document(request).model_dump(exclude_none=True, by_alias=True),
            content_type=AUTHENTICATION_DOCUMENT_TYPE,
        )

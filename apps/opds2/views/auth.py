from http import HTTPStatus
from typing import Optional

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views import View

from apps.core.errors import ProblemDetailException
from apps.core.models import Catalog
from apps.opds2.schema.opds import AuthenticationDocument, AuthenticationFlow
from apps.opds2.schema.rwpm import Link

AUTHENTICATION_DOCUMENT_TYPE = "application/opds-authentication+json"
OPDS_JSON = "application/opds+json"
REL_AUTH_DOCUMENT = "http://opds-spec.org/auth/document"
REL_SHELF = "http://opds-spec.org/shelf"
DEFAULT_TITLE = "Evil Flowers Catalog"


def authentication_document_url(request, catalog: Optional[Catalog] = None) -> str:
    """Absolute URL of the catalog's Authentication Document (the global one without a catalog)."""
    if catalog is None:
        path = reverse("opds2:auth")
    else:
        path = reverse("opds2:catalog-auth", kwargs={"catalog_name": catalog.url_name})
    return f"{request.scheme}://{request.get_host()}{path}"


def authentication_document(request, catalog: Optional[Catalog] = None) -> AuthenticationDocument:
    """Authentication for OPDS 1.0 document, scoped to `catalog` when given.

    A catalog's document links that catalog's shelf
    (`rel="http://opds-spec.org/shelf"`, `application/opds+json`). Thorium
    stores the auth document it finds in a feed's root, and its catalog-card
    person icon opens the shelf link from that document. Without the link the
    icon only reopens the sign-in dialog. The shelf answers an anonymous
    request with a `401`, which is what brings up sign-in.
    """
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

    links = None
    title = DEFAULT_TITLE
    if catalog is not None:
        kwargs = {"catalog_name": catalog.url_name}
        links = [
            Link(href=f"{base_url}{reverse('opds2:shelf', kwargs=kwargs)}", type=OPDS_JSON, rel=REL_SHELF),
            Link(href=f"{base_url}{reverse('opds2:catalog', kwargs=kwargs)}", type=OPDS_JSON, rel="start"),
        ]
        # A private catalog's title is not shown to someone who has not signed in.
        if catalog.is_public:
            title = catalog.title

    return AuthenticationDocument(
        id=authentication_document_url(request, catalog),
        title=title,
        description="Sign in to borrow publications",
        links=links,
        authentication=flows,
    )


def authentication_required(request, catalog: Optional[Catalog] = None) -> JsonResponse:
    """`401` whose body is the Authentication Document.

    Authentication for OPDS 1.0 §2.1: a catalog answers an unauthenticated
    (or wrongly authenticated) request with the document itself, so the
    reader can show its sign-in dialog. Thorium reads a `problem+json` body
    as a plain error and never offers to sign in, so OPDS endpoints must not
    answer a 401 with one.
    """
    document = authentication_document(request, catalog)
    response = JsonResponse(
        document.model_dump(exclude_none=True, by_alias=True),
        status=HTTPStatus.UNAUTHORIZED,
        content_type=AUTHENTICATION_DOCUMENT_TYPE,
    )
    realm = slugify(settings.INSTANCE_NAME)
    response["WWW-Authenticate"] = f'Basic realm="{realm}"'
    response["Link"] = f'<{document.id}>; rel="{REL_AUTH_DOCUMENT}"; type="{AUTHENTICATION_DOCUMENT_TYPE}"'
    return response


def _document_response(request, catalog: Optional[Catalog] = None) -> JsonResponse:
    return JsonResponse(
        authentication_document(request, catalog).model_dump(exclude_none=True, by_alias=True),
        content_type=AUTHENTICATION_DOCUMENT_TYPE,
    )


class AuthenticationDocumentView(View):
    """The catalog-independent document (kept for clients that saved its URL)."""

    def get(self, request):
        return _document_response(request)


class CatalogAuthenticationDocumentView(View):
    """A catalog's Authentication Document. Never requires authentication:
    it is how a reader learns to authenticate, private catalogs included."""

    def get(self, request, catalog_name: str):
        try:
            catalog = Catalog.objects.get(url_name=catalog_name)
        except Catalog.DoesNotExist:
            raise ProblemDetailException(_("Catalog not found"), status=HTTPStatus.NOT_FOUND)
        return _document_response(request, catalog)

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.views import View

from apps.opds2.schema.opds import AuthenticationDocument, AuthenticationFlow
from apps.opds2.schema.rwpm import Link


class AuthenticationDocumentView(View):
    def get(self, request):
        base_url = f"{request.scheme}://{request.get_host()}"

        flows = [
            AuthenticationFlow(
                type="http://opds-spec.org/auth/basic",
                labels={"login": "Username or Email", "password": "Password"},
            ),
        ]

        # Add Bearer token flow if JWT auth is available
        flows.append(
            AuthenticationFlow(
                type="http://opds-spec.org/auth/bearer",
                links=[
                    Link(
                        href=f"{base_url}{reverse('api:login')}",
                        type="application/json",
                        rel="authenticate",
                    ),
                ],
            )
        )

        doc = AuthenticationDocument(
            id=f"{base_url}{reverse('opds2:auth')}",
            title="Evil Flowers Catalog",
            description="Sign in to borrow publications",
            authentication=flows,
        )

        return JsonResponse(
            doc.model_dump(exclude_none=True, by_alias=True),
            content_type="application/opds-authentication+json",
        )

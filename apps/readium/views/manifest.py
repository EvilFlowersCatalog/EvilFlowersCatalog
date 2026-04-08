"""
Publication Manifest View

Serves RWPM (Readium Web Publication Manifest) for publications.
LCP readers use this to get full publication metadata.
"""

from http import HTTPStatus
from uuid import UUID

from django.http import JsonResponse
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.core.models import Entry
from apps.core.views import SecuredView
from apps.opds2.services import ManifestBuilder


class PublicationManifestView(SecuredView):
    def get(self, request, entry_id: UUID):
        try:
            entry = (
                Entry.objects.select_related("catalog", "language")
                .prefetch_related("entry_authors__author", "categories", "acquisitions")
                .get(pk=entry_id)
            )
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        base_url = f"{request.scheme}://{request.get_host()}"
        manifest = ManifestBuilder.build_manifest(entry, base_url=base_url)

        return JsonResponse(
            manifest.model_dump(exclude_none=True, by_alias=True),
            content_type="application/webpub+json",
        )

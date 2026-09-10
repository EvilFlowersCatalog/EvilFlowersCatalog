from http import HTTPStatus
from uuid import UUID

from django.http import JsonResponse
from django.utils.translation import gettext as _

from apps.api.utils.parse import parse_int_query
from apps.core.errors import ProblemDetailException
from apps.core.models import Entry
from apps.opds2.services import FeedBuilder, ManifestBuilder
from apps.opds2.views.base import Opds2CatalogView


class PublicationFeedView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by("-created_at")
        page = parse_int_query(request, "page", default=1, min_value=1)
        per_page = parse_int_query(request, "per_page", default=0, min_value=0) or None

        feed = FeedBuilder.build_publication_feed(
            entries, self.catalog, request, page=page, per_page=per_page, title=f"{self.catalog.title} - Publications"
        )
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))


class PublicationDetailView(Opds2CatalogView):
    def get(self, request, catalog_name: str, entry_id: UUID):
        try:
            entry = (
                Entry.objects.select_related("catalog", "language")
                .prefetch_related("entry_authors__author", "categories", "acquisitions")
                .get(pk=entry_id, catalog=self.catalog)
            )
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        base_url = f"{request.scheme}://{request.get_host()}"
        publication = ManifestBuilder.build_publication(entry, base_url=base_url)
        return self.opds_response(publication.model_dump(exclude_none=True, by_alias=True))


class PublicationManifestView(Opds2CatalogView):
    def get(self, request, catalog_name: str, entry_id: UUID):
        try:
            entry = (
                Entry.objects.select_related("catalog", "language")
                .prefetch_related("entry_authors__author", "categories", "acquisitions")
                .get(pk=entry_id, catalog=self.catalog)
            )
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Publication not found"), status=HTTPStatus.NOT_FOUND)

        base_url = f"{request.scheme}://{request.get_host()}"
        manifest = ManifestBuilder.build_manifest(entry, base_url=base_url)
        return JsonResponse(
            manifest.model_dump(exclude_none=True, by_alias=True),
            content_type="application/webpub+json",
        )

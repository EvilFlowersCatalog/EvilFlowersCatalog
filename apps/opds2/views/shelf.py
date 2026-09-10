from http import HTTPStatus

from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException, UnauthorizedException
from apps.opds2.services import FeedBuilder
from apps.opds2.views.base import Opds2CatalogView


class ShelfView(Opds2CatalogView):
    def get(self, request, catalog_name: str):
        if not request.user.is_authenticated:
            raise UnauthorizedException()

        feed = FeedBuilder.build_shelf_feed(request.user, self.catalog, request)
        return self.opds_response(feed.model_dump(exclude_none=True, by_alias=True))

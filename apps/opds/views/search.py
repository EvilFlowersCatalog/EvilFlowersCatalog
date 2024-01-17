from http import HTTPStatus

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.api.filters.entries import EntryFilter
from apps.core.errors import ProblemDetailException
from apps.core.models import Feed, Catalog


class SearchView(View):
    def get(self, request, catalog_name: str, feed_name: str = None):
        try:
            catalog = Catalog.objects.get(url_name=catalog_name)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(
                request, _("Feed not found"), status=HTTPStatus.NOT_FOUND, previous=e
            )

        tags = {
            "short_name": catalog.title,
            "description": "OPDS catalog",
            "filter": EntryFilter,
            "url": f"{settings.BASE_URL}"
            f"{reverse('root', kwargs={'catalog_name': catalog.url_name})}"
            f"?{EntryFilter.template()}",
        }

        if feed_name:
            try:
                feed = Feed.objects.get(catalog=catalog, url_name=feed_name)
            except Feed.DoesNotExist as e:
                raise ProblemDetailException(
                    request,
                    _("Feed not found"),
                    status=HTTPStatus.NOT_FOUND,
                    previous=e,
                )

            tags["short_name"] = f"{feed.title} - {catalog.title}"
            tags["description"] = f"{feed.title} feed"
            tags["url"] = (
                f"{settings.BASE_URL}"
                f"{reverse('feed', kwargs={'catalog_name': catalog.url_name, 'feed_name': feed.url_name})}"
                f"?{EntryFilter.template()}"
            )

        return render(
            request,
            "opds/search.xml",
            tags,
            content_type="application/opensearchdescription+xml",
        )

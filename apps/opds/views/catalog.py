from http import HTTPStatus

from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views import View

from apps.api.views.base import SecuredView
from apps.core.errors import ProblemDetailException
from apps.core.models import Catalog, Feed


class OpdsView(SecuredView):
    def __init__(self, *args, **kwargs):
        self.catalog = None
        super().__init__(*args, **kwargs)

    def dispatch(self, request, *args, **kwargs):
        try:
            self.catalog = Catalog.objects.get(url_name=kwargs['catalog_name'])
        except Catalog.DoesNotExist:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND)
        except KeyError as e:
            raise ProblemDetailException(request, _("Internal server error"), status=HTTPStatus.NOT_FOUND, previous=e)

        if request.method not in self.UNSECURED_METHODS and not self.catalog.is_public:
            self._authenticate(request)

        return View.dispatch(self, request, *args, **kwargs)


class RootView(OpdsView):
    def get(self, request, catalog_name: str):
        feeds = self.catalog.feeds.filter(parents__content__isnull=True)
        return render(request, 'opds/root.xml', {
            'catalog': self.catalog,
            'feeds': feeds
        }, content_type='application/atom+xml;profile=opds-catalog')


class FeedView(OpdsView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            return HttpResponse("Feed not found", status=HTTPStatus.NOT_FOUND)

        return render(request, 'opds/feed.xml', {
            'feed': feed
        }, content_type=f'application/atom+xml;profile=opds-catalog;kind={feed.kind}')


class PopularFeedView(OpdsView):
    def get(self):
        return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)


class ShelfFeedView(OpdsView):
    def get(self):
        return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)


class LatestFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        # entries = Entry.objects.filter(catalog=self.catalog).order_by('created_at')[settings.OPDS['NEW_LIMIT']]

        # return render(request, f'opds/new_feed.xml', {
        #     'entries': entries
        # }, content_type='application/atom+xml;profile=opds-catalog')

        return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)

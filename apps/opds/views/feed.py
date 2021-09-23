from http import HTTPStatus

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _

from apps.api.filters.entries import EntryFilter
from apps.core.errors import ProblemDetailException
from apps.core.models import Feed, Entry
from apps.opds.views.base import OpdsView


class FeedView(OpdsView):
    def get(self, request, catalog_name: str, feed_name: str):
        try:
            feed = Feed.objects.get(catalog=self.catalog, url_name=feed_name)
        except Feed.DoesNotExist:
            raise ProblemDetailException(request, _("Feed not found"), status=HTTPStatus.NOT_FOUND)

        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.filter(feeds=feed), request=request)

        try:
            updated_at = entry_filter.qs.latest('updated_at').updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(request, 'opds/feeds/feed.xml', {
            'feed': feed,
            'updated_at': updated_at,
            'catalog': self.catalog,
            'entry_filter': entry_filter
        }, content_type=f'application/atom+xml;profile=opds-catalog;kind={feed.kind}')


class CompleteFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request)

        try:
            updated_at = entry_filter.qs.latest('updated_at').updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(request, 'opds/feeds/complete.xml', {
            'catalog': self.catalog,
            'updated_at': updated_at,
            'entry_filter': entry_filter
        }, content_type='application/atom+xml;profile=opds-catalog;kind=acquisition')


class LatestFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by('created_at')[:settings.OPDS['NEW_LIMIT']]
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request)

        try:
            updated_at = Entry.objects.filter(id__in=entries).latest('updated_at').updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(request, 'opds/feeds/latest.xml', {
            'catalog': self.catalog,
            'updated_at': updated_at,
            'entry_filter': entry_filter
        }, content_type='application/atom+xml;profile=opds-catalog;kind=acquisition')


class PopularFeedView(OpdsView):
    def get(self, request, catalog_name: str):
        entries = Entry.objects.filter(catalog=self.catalog).order_by('-popularity')[:settings.OPDS['NEW_LIMIT']]
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.filter(pk__in=entries), request=request)

        try:
            updated_at = Entry.objects.filter(id__in=entries).latest('updated_at').updated_at
        except Entry.DoesNotExist:
            updated_at = self.catalog.updated_at

        return render(request, 'opds/feeds/popular.xml', {
            'catalog': self.catalog,
            'updated_at': updated_at,
            'entry_filter': entry_filter
        }, content_type='application/atom+xml;profile=opds-catalog;kind=acquisition')


class ShelfFeedView(OpdsView):
    def get(self):
        return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)

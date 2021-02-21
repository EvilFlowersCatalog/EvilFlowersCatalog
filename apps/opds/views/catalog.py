from http import HTTPStatus

from django.http import HttpResponse
from django.shortcuts import render

from apps.core.models import Catalog, Feed


def root(request, catalog_name: str):
    try:
        catalog = Catalog.objects.get(url_name=catalog_name)
    except Catalog.DoesNotExist:
        return HttpResponse("Catalog not found", status=HTTPStatus.NOT_FOUND)

    return render(request, 'opds/root.xml', {
        'catalog': catalog
    }, content_type='application/atom+xml;profile=opds-catalog')


def subsection(request, catalog_name: str, feed_name: str):
    try:
        feed = Feed.objects.get(catalog__url_name=catalog_name, url_name=feed_name)
    except Feed.DoesNotExist:
        return HttpResponse("Feed not found", status=HTTPStatus.NOT_FOUND)

    return render(request, 'opds/feed.xml', {
        'feed': feed
    }, content_type='application/atom+xml;profile=opds-catalog')


def feed_popular(request, catalog_name: str):
    return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)


def feed_new(request, catalog_name: str):
    return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)


def feed_shelf(request, catalog_name: str):
    return HttpResponse("Not implemented", status=HTTPStatus.NOT_IMPLEMENTED)

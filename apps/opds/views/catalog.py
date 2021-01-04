from http import HTTPStatus

from django.http import HttpResponse
from django.shortcuts import render

from apps.core.models import Catalog


def catalog_detail(request, url_name: str):
    try:
        catalog = Catalog.objects.get(url_name=url_name)
    except Catalog.DoesNotExist:
        return HttpResponse("Catalog not found", status=HTTPStatus.NOT_FOUND)

    return render(request, 'opds/catalog.xml', {
        'catalog': catalog
    }, content_type='application/atom+xml;profile=opds-catalog')

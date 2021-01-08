from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from django.views.generic.base import View

from apps.api.errors import ValidationException, ApiException
from apps.api.filters.catalogs import CatalogFilter
from apps.api.forms.catalogs import CatalogForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.catalogs import CatalogSerializer
from apps.core.models import Catalog


class CatalogManagement(View):
    require_apikey = ['post', 'get']

    def post(self, request):
        form = CatalogForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if Catalog.objects.filter(url_name=form.cleaned_data['url_name']).exists():
            raise ApiException(request, message=_('Catalog url_name already taken'), status_code=HTTPStatus.CONFLICT)

        catalog = Catalog(creator=request.api_key.user)
        form.fill(catalog)
        catalog.save()

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Base, status=HTTPStatus.CREATED)

    def get(self, request):
        catalogs = CatalogFilter(request.GET, queryset=Catalog.objects.all(), request=request).qs

        return PaginationResponse(
            request, catalogs, page=request.GET.get('page', 1), serializer=CatalogSerializer.Base
        )


class CatalogDetail(View):
    require_apikey = ['put', 'get', 'delete']

    @staticmethod
    def _get_catalog(request, catalog_id: UUID) -> Catalog:
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ApiException(request, _("Catalog not found"), status_code=HTTPStatus.NOT_FOUND, previous=e)

        if not request.api_key.user.is_superuser and catalog.creator_id != request.api_key.user_id:
            raise ApiException(request, _("Insufficient permissions"), status_code=HTTPStatus.FORBIDDEN)

        return catalog

    def get(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id)

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Base)

    def put(self, request, catalog_id: UUID):
        form = CatalogForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        catalog = self._get_catalog(request, catalog_id)

        if Catalog.objects.exclude(pk=catalog.pk).filter(url_name=form.cleaned_data['url_name']).exists():
            raise ApiException(request, message=_('Catalog url_name already taken'), status_code=HTTPStatus.CONFLICT)

        form.fill(catalog)
        catalog.save()

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Base)

    def delete(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id)
        catalog.hard_delete()

        return SingleResponse(request)

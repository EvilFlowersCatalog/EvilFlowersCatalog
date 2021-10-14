from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.catalogs import CatalogFilter
from apps.api.forms.catalogs import CatalogForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.catalogs import CatalogSerializer
from apps.api.views.base import SecuredView
from apps.core.models import Catalog


class CatalogManagement(SecuredView):
    def post(self, request):
        form = CatalogForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if Catalog.objects.filter(url_name=form.cleaned_data['url_name']).exists():
            raise ProblemDetailException(
                request, title=_('Catalog url_name already taken'), status=HTTPStatus.CONFLICT
            )

        catalog = Catalog(creator=request.user)
        form.populate(catalog)
        catalog.save()

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Base, status=HTTPStatus.CREATED)

    def get(self, request):
        catalogs = CatalogFilter(request.GET, queryset=Catalog.objects.all(), request=request).qs

        return PaginationResponse(request, catalogs, serializer=CatalogSerializer.Base)


class CatalogDetail(SecuredView):
    @staticmethod
    def _get_catalog(request, catalog_id: UUID) -> Catalog:
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not request.user.is_superuser and catalog.creator_id != request.user.id:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

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
            raise ProblemDetailException(
                request, title=_('Catalog url_name already taken'), status=HTTPStatus.CONFLICT
            )

        form.populate(catalog)
        catalog.save()

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Base)

    def delete(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id)
        catalog.hard_delete()

        return SingleResponse(request)

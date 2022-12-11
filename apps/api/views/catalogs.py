from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.api.services.catalog import CatalogService
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.catalogs import CatalogFilter
from apps.api.forms.catalogs import CatalogForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.catalogs import CatalogSerializer
from apps.core.models import Catalog, UserCatalog
from apps.core.views import SecuredView


class CatalogManagement(SecuredView):
    def post(self, request):
        form = CatalogForm.create_from_request(request)

        if not request.user.has_perm('core.add_catalog'):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if not form.is_valid():
            raise ValidationException(request, form)

        if Catalog.objects.filter(url_name=form.cleaned_data['url_name']).exists():
            raise ProblemDetailException(
                request, title=_('Catalog url_name already taken'), status=HTTPStatus.CONFLICT
            )

        service = CatalogService()
        catalog = service.populate(
            catalog=Catalog(creator=request.user),
            form=form
        )

        if not catalog.users.contains(request.user):
            UserCatalog.objects.create(
                catalog=catalog,
                user=request.user,
                mode=UserCatalog.Mode.MANAGE
            )

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Detailed, status=HTTPStatus.CREATED)

    def get(self, request):
        catalogs = CatalogFilter(request.GET, queryset=Catalog.objects.all(), request=request).qs

        return PaginationResponse(request, catalogs, serializer=CatalogSerializer.Base)


class CatalogDetail(SecuredView):
    @staticmethod
    def _get_catalog(request, catalog_id: UUID, checker: str = 'check_catalog_manage') -> Catalog:
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(request, _("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission(checker, request.user, catalog):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return catalog

    def get(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id, 'check_catalog_read')

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Detailed)

    def put(self, request, catalog_id: UUID):
        form = CatalogForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        catalog = self._get_catalog(request, catalog_id)

        if Catalog.objects.exclude(pk=catalog.pk).filter(url_name=form.cleaned_data['url_name']).exists():
            raise ProblemDetailException(
                request, title=_('Catalog url_name already taken'), status=HTTPStatus.CONFLICT
            )

        service = CatalogService()
        catalog = service.populate(
            catalog=catalog,
            form=form
        )

        return SingleResponse(request, catalog, serializer=CatalogSerializer.Detailed)

    def delete(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id)
        catalog.delete()

        return SingleResponse(request)

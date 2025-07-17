from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.services.catalog import CatalogService
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.catalogs import CatalogFilter
from apps.api.forms.catalogs import CatalogForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.catalogs import CatalogSerializer
from apps.core.models import Catalog, UserCatalog
from apps.core.views import SecuredView


class CatalogManagement(SecuredView):
    @openapi.metadata(
        description="Create a new catalog to organize and manage publications. A catalog is a container for entries (books, articles, etc.) and can be configured with access permissions, visibility settings, and custom metadata.",
        tags=["Catalogs"],
        summary="Create a new catalog",
    )
    def post(self, request):
        form = CatalogForm.create_from_request(request)

        if not request.user.has_perm("core.add_catalog"):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if not form.is_valid():
            raise ValidationException(form)

        if Catalog.objects.filter(url_name=form.cleaned_data["url_name"]).exists():
            raise ProblemDetailException(
                title=_("Catalog url_name already taken"),
                status=HTTPStatus.CONFLICT,
            )

        service = CatalogService()
        catalog = service.populate(catalog=Catalog(creator=request.user), form=form)

        if not catalog.users.contains(request.user):
            UserCatalog.objects.create(catalog=catalog, user=request.user, mode=UserCatalog.Mode.MANAGE)

        return SingleResponse(
            request,
            data=CatalogSerializer.Detailed.model_validate(catalog),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description="Retrieve a paginated list of catalogs accessible to the authenticated user. Supports filtering by title and URL name. Returns both public catalogs and private catalogs the user has access to.",
        tags=["Catalogs"],
        summary="List accessible catalogs",
    )
    def get(self, request):
        catalogs = CatalogFilter(request.GET, queryset=Catalog.objects.all(), request=request).qs.select_related(
            "creator"
        )

        return PaginationResponse(request, catalogs, serializer=CatalogSerializer.Base)


class CatalogDetail(SecuredView):
    @staticmethod
    def _get_catalog(request, catalog_id: UUID, checker: str = "check_catalog_manage") -> Catalog:
        try:
            catalog = Catalog.objects.select_related("creator").prefetch_related("users").get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(_("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission(checker, request.user, catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return catalog

    @openapi.metadata(
        description="Retrieve detailed information about a specific catalog, including its metadata, statistics, and access permissions. This endpoint provides comprehensive information about the catalog's configuration and contents.",
        tags=["Catalogs"],
        summary="Get catalog details",
    )
    def get(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id, "check_catalog_read")

        return SingleResponse(request, data=CatalogSerializer.Detailed.model_validate(catalog))

    @openapi.metadata(
        description="Update the metadata and configuration of an existing catalog. This includes title, description, visibility settings, and access permissions. Only users with manage permissions can update a catalog.",
        tags=["Catalogs"],
        summary="Update catalog metadata",
    )
    def put(self, request, catalog_id: UUID):
        form = CatalogForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(form)

        catalog = self._get_catalog(request, catalog_id)

        if Catalog.objects.exclude(pk=catalog.pk).filter(url_name=form.cleaned_data["url_name"]).exists():
            raise ProblemDetailException(
                title=_("Catalog url_name already taken"),
                status=HTTPStatus.CONFLICT,
            )

        service = CatalogService()
        catalog = service.populate(catalog=catalog, form=form)

        return SingleResponse(request, data=CatalogSerializer.Detailed.model_validate(catalog))

    @openapi.metadata(
        description="Permanently delete a catalog and all its associated entries, acquisition files, and user data. This action is irreversible and requires appropriate permissions. All content within the catalog will be lost.",
        tags=["Catalogs"],
        summary="Delete catalog",
    )
    def delete(self, request, catalog_id: UUID):
        catalog = self._get_catalog(request, catalog_id)
        catalog.delete()

        return SingleResponse(request)

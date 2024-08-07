from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.filters.categories import CategoryFilter
from apps.api.forms.category import CategoryForm
from apps.api.serializers.entries import CategorySerializer
from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.response import SingleResponse, PaginationResponse
from apps.core.models import Category
from apps.core.views import SecuredView


class CategoryManagement(SecuredView):
    @openapi.metadata(description="Create Category", tags=["Categories"])
    def post(self, request):
        form = CategoryForm.create_from_request(request)
        form.fields["catalog_id"].required = True

        if not form.is_valid():
            raise ValidationException(form)

        if not has_object_permission("check_catalog_manage", request.user, form.cleaned_data["catalog_id"]):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if Category.objects.filter(term=form.cleaned_data["term"]).exists():
            raise ProblemDetailException(
                title=_("Category with term %s is already taken in catalog") % (form.cleaned_data["term"],),
                status=HTTPStatus.CONFLICT,
            )

        category = Category(creator=request.user)
        form.populate(category)
        category.save()

        return SingleResponse(
            request,
            data=CategorySerializer.Detailed.model_validate(category),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(description="List Categories", tags=["Categories"])
    def get(self, request):
        catalogs = CategoryFilter(request.GET, queryset=Category.objects.all(), request=request).qs

        return PaginationResponse(request, catalogs, serializer=CategorySerializer.Detailed)


class CategoryDetail(SecuredView):
    @staticmethod
    def _get_category(request, category_id: UUID, checker: str = "check_catalog_manage") -> Category:
        try:
            category = Category.objects.get(pk=category_id)
        except Category.DoesNotExist as e:
            raise ProblemDetailException(
                _("Category not found"),
                status=HTTPStatus.NOT_FOUND,
                previous=e,
            )

        if not has_object_permission(checker, request.user, category.catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return category

    @openapi.metadata(description="Get Category detail", tags=["Categories"])
    def get(self, request, category_id: UUID):
        category = self._get_category(request, category_id, "check_catalog_read")

        return SingleResponse(request, data=CategorySerializer.Detailed.model_validate(category))

    @openapi.metadata(description="Update Category", tags=["Categories"])
    def put(self, request, category_id: UUID):
        form = CategoryForm.create_from_request(request)
        form.fields["catalog_id"].required = True

        if not form.is_valid():
            raise ValidationException(form)

        category = self._get_category(request, category_id)

        if not has_object_permission("check_catalog_manage", request.user, form.cleaned_data["catalog_id"]):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if Category.objects.exclude(pk=category.pk).filter(term=form.cleaned_data["term"]).exists():
            raise ProblemDetailException(
                title=_("Category with term %s is already taken in catalog") % (form.cleaned_data["term"],),
                status=HTTPStatus.CONFLICT,
            )

        form.populate(category)
        category.save()

        return SingleResponse(request, data=CategorySerializer.Detailed.model_validate(category))

    @openapi.metadata(description="Delete Category", tags=["Categories"])
    def delete(self, request, category_id: UUID):
        category = self._get_category(request, category_id)
        category.delete()

        return SingleResponse(request)

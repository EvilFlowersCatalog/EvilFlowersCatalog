from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps.core.errors import ValidationException, ProblemDetailException
from apps.api.filters.authors import AuthorFilter
from apps.api.forms.entries import CreateAuthorForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.entries import AuthorSerializer
from apps.core.models import Author
from apps.core.views import SecuredView


class AuthorManagement(SecuredView):
    def get(self, request):
        feeds = AuthorFilter(request.GET, queryset=Author.objects.all(), request=request).qs

        return PaginationResponse(request, feeds, serializer=AuthorSerializer.Detailed)

    def post(self, request):
        form = CreateAuthorForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if not has_object_permission("check_catalog_write", request.user, form.cleaned_data["catalog_id"]):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if Author.objects.filter(
            catalog=form.cleaned_data["catalog_id"],
            name=form.cleaned_data["name"],
            surname=form.cleaned_data["surname"],
        ).exists():
            raise ProblemDetailException(
                request, _("Author already exists in the catalog"), status=HTTPStatus.CONFLICT
            )

        author = Author()
        form.populate(author)
        author.save()

        return SingleResponse(request, author, serializer=AuthorSerializer.Detailed, status=HTTPStatus.CREATED)


class AuthorDetail(SecuredView):
    @staticmethod
    def _get_author(request, author_id: UUID, checker: str = "check_catalog_manage") -> Author:
        try:
            author = Author.objects.select_related("catalog").get(pk=author_id)
        except Author.DoesNotExist as e:
            raise ProblemDetailException(request, _("Author not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission(checker, request.user, author.catalog):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return author

    def get(self, request, author_id: UUID):
        author = self._get_author(request, author_id, "check_catalog_read")

        return SingleResponse(request, author, serializer=AuthorSerializer.Detailed)

    def put(self, request, author_id: UUID):
        form = CreateAuthorForm.create_from_request(request)
        author = self._get_author(request, author_id)

        if not form.is_valid():
            raise ValidationException(request, form)

        if (
            Author.objects.exclude(pk=author_id)
            .filter(
                catalog=form.cleaned_data["catalog_id"],
                name=form.cleaned_data["name"],
                surname=form.cleaned_data["surname"],
            )
            .exists()
        ):
            raise ProblemDetailException(
                request, _("Author already exists in the catalog"), status=HTTPStatus.CONFLICT
            )

        form.populate(author)
        author.save()

        return SingleResponse(request, author, serializer=AuthorSerializer.Detailed)

    def delete(self, request, author_id: UUID):
        author = self._get_author(request, author_id)
        author.delete()

        return SingleResponse(request)

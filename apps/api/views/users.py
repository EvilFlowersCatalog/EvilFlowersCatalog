from http import HTTPStatus
from typing import Optional, Callable
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.core.errors import (
    ValidationException,
    ProblemDetailException,
    UnauthorizedException,
)
from apps.api.filters.users import UserFilter
from apps.api.forms.users import UserForm, CreateUserForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.users import UserSerializer
from apps.core.models import User, AuthSource
from apps.core.views import SecuredView


class UserManagement(SecuredView):
    @openapi.metadata(
        description="Create a new user account with authentication credentials and profile information. The user will be able to authenticate and access catalogs based on assigned permissions.",
        tags=["Users"],
        summary="Create a new user account"
    )
    def post(self, request):
        form = CreateUserForm.create_from_request(request)

        if not request.user.has_perm("core.add_user"):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if not form.is_valid():
            raise ValidationException(form)

        if User.objects.filter(username=form.cleaned_data["username"]).exists():
            raise ProblemDetailException(_("User with same username already exists"), status=HTTPStatus.CONFLICT)

        user = User(auth_source=AuthSource.objects.filter(driver=AuthSource.Driver.DATABASE).first())
        form.populate(user)
        user.set_password(form.cleaned_data["password"])
        user.save()

        return SingleResponse(request, data=UserSerializer.Base.model_validate(user), status=HTTPStatus.CREATED)

    @openapi.metadata(
        description="Retrieve a paginated list of users in the system. Supports filtering by username, name, surname, active status, and last login date. Requires appropriate permissions to access user information.",
        tags=["Users"],
        summary="List system users"
    )
    def get(self, request):
        users = UserFilter(request.GET, queryset=User.objects.all(), request=request).qs

        return PaginationResponse(request, users, serializer=UserSerializer.Base)


class UserDetail(SecuredView):
    @staticmethod
    def _get_user(request, user_id: UUID, perm_test: Optional[Callable] = None) -> User:
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist as e:
            raise ProblemDetailException(_("User not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not perm_test:
            perm_test = lambda: True

        if not (user.id == request.user.id or perm_test()):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return user

    @openapi.metadata(
        description="Retrieve detailed information about a specific user, including their profile information, permissions, and catalog access. Requires appropriate permissions to view user details.",
        tags=["Users"],
        summary="Get user details"
    )
    def get(self, request, user_id: UUID):
        user = self._get_user(request, user_id, lambda: request.user.has_perm("core.view_user"))

        return SingleResponse(request, data=UserSerializer.Detailed.model_validate(user))

    @openapi.metadata(
        description="Update user profile information including name, surname, email, and active status. Requires appropriate permissions to modify user accounts.",
        tags=["Users"],
        summary="Update user profile"
    )
    def put(self, request, user_id: UUID):
        form = UserForm.create_from_request(request)

        user = self._get_user(request, user_id, lambda: request.user.has_perm("core.change_user"))

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(user)
        if "password" in form.cleaned_data.keys():
            user.set_password(form.cleaned_data["password"])
        user.save()

        return SingleResponse(request, data=UserSerializer.Base.model_validate(user))

    @openapi.metadata(
        description="Permanently delete a user account and all associated data including annotations, shelf records, and access permissions. This action is irreversible and requires appropriate permissions.",
        tags=["Users"],
        summary="Delete user account"
    )
    def delete(self, request, user_id: UUID):
        user = self._get_user(request, user_id, lambda: request.user.has_perm("core.delete_user"))
        user.delete()

        return SingleResponse(request)


class UserMe(SecuredView):
    @openapi.metadata(
        description="Get detailed information about the currently authenticated user including profile data, permissions, and accessible catalogs. This endpoint allows users to view their own account information.",
        tags=["Users"],
        summary="Get current user profile"
    )
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You have to log in!"))

        return SingleResponse(request, data=UserSerializer.Detailed.model_validate(request.user))

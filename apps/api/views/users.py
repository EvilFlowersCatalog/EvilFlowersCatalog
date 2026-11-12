from http import HTTPStatus
from typing import Optional, Callable
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.core.errors import (
    ValidationException,
    ProblemDetailException,
    UnauthorizedException,
    DetailType,
)
from apps.api.filters.users import UserFilter
from apps.api.forms.users import UserForm, CreateUserForm, PasswordChangeForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.users import UserSerializer
from apps.core.models import User, AuthSource
from apps.core.views import SecuredView


def _apply_role(request, form, user: User) -> None:
    """Grant or revoke the administrator role. Only superusers may do it, and never on themselves."""
    if "is_superuser" not in form.cleaned_data or form.cleaned_data["is_superuser"] == user.is_superuser:
        return

    if not request.user.is_superuser:
        raise ProblemDetailException(_("Only administrators can change roles"), status=HTTPStatus.FORBIDDEN)

    if user.pk == request.user.pk:
        raise ProblemDetailException(_("You cannot change your own role"), status=HTTPStatus.CONFLICT)

    user.is_superuser = form.cleaned_data["is_superuser"]


class UserManagement(SecuredView):
    @openapi.metadata(
        description="Create a new user account with authentication credentials and profile information. The user will be able to authenticate and access catalogs based on assigned permissions.",
        tags=["Users"],
        summary="Create a new user account",
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
        _apply_role(request, form, user)
        user.password_change_required = bool(form.cleaned_data.get("password_change_required"))
        user.save()

        return SingleResponse(request, data=UserSerializer.Base.model_validate(user), status=HTTPStatus.CREATED)

    @openapi.metadata(
        description="Retrieve a paginated list of users in the system. Supports filtering by username, name, surname, active status, and last login date. Requires appropriate permissions to access user information.",
        tags=["Users"],
        summary="List system users",
    )
    def get(self, request):
        users = UserFilter(request.GET, queryset=User.objects.all(), request=request).qs

        return PaginationResponse(request, users, serializer=UserSerializer.Base)


class UserDetail(SecuredView):
    @staticmethod
    def _get_user(request, user_id: UUID, perm_test: Optional[Callable] = None) -> User:
        """The account, if the caller may act on it.

        An account the caller may not even *view* answers 404, exactly like a
        missing id — answering 403 would confirm which ids are real accounts.
        Only a caller who can see the account is told they may not change it.
        """
        user = User.objects.filter(pk=user_id).first()
        is_self = user is not None and user.pk == request.user.pk

        if user is None or not (is_self or request.user.has_perm("core.view_user")):
            raise ProblemDetailException(_("User not found"), status=HTTPStatus.NOT_FOUND)

        if is_self or perm_test is None:
            return user

        if not perm_test():
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        # `core.change_user` / `core.delete_user` without superuser rights must
        # not reach an administrator: resetting their password is a takeover.
        if user.is_superuser and not request.user.is_superuser:
            raise ProblemDetailException(
                _("Only a superuser may modify an administrator account"), status=HTTPStatus.FORBIDDEN
            )

        return user

    @openapi.metadata(
        description="Retrieve detailed information about a specific user, including their profile information, permissions, and catalog access. Requires appropriate permissions to view user details.",
        tags=["Users"],
        summary="Get user details",
    )
    def get(self, request, user_id: UUID):
        # Visibility (`core.view_user` or self) is checked inside `_get_user`.
        user = self._get_user(request, user_id)

        return SingleResponse(request, data=UserSerializer.Detailed.model_validate(user))

    @openapi.metadata(
        description="Update user profile information including name, surname, email, active status, and LCP passphrase. Requires appropriate permissions to modify user accounts.",
        tags=["Users"],
        summary="Update user profile",
    )
    def put(self, request, user_id: UUID):
        form = UserForm.create_from_request(request)

        user = self._get_user(request, user_id, lambda: request.user.has_perm("core.change_user"))

        if not form.is_valid():
            raise ValidationException(form)

        form.populate(user)
        if form.cleaned_data.get("password"):
            user.set_password(form.cleaned_data["password"])
        _apply_role(request, form, user)
        # An administrator may require someone else to change their password (#8); a user cannot lift it
        # for themselves this way — only by changing the password.
        if "password_change_required" in form.cleaned_data and user.pk != request.user.pk:
            if form.cleaned_data["password_change_required"] is not None:
                user.password_change_required = bool(form.cleaned_data["password_change_required"])

        # Handle LCP passphrase - hash before storing
        if "lcp_passphrase" in form.cleaned_data and form.cleaned_data["lcp_passphrase"]:
            from apps.readium.services.lcp_server_client import LCPServerClient

            user.lcp_passphrase_hash = LCPServerClient.hash_passphrase(form.cleaned_data["lcp_passphrase"])

        user.save()

        return SingleResponse(request, data=UserSerializer.Base.model_validate(user))

    @openapi.metadata(
        description="Permanently delete a user account and all associated data including annotations, shelf records, and access permissions. This action is irreversible and requires appropriate permissions.",
        tags=["Users"],
        summary="Delete user account",
    )
    def delete(self, request, user_id: UUID):
        user = self._get_user(request, user_id, lambda: request.user.has_perm("core.delete_user"))
        user.delete()

        return SingleResponse(request)


class UserMe(SecuredView):
    # The client reads `password_change_required` here to know it must ask for a new password (#8).
    allowed_while_password_change_required = True

    @openapi.metadata(
        description="Get detailed information about the currently authenticated user including profile data, permissions, and accessible catalogs. This endpoint allows users to view their own account information.",
        tags=["Users"],
        summary="Get current user profile",
    )
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You have to log in!"))

        return SingleResponse(request, data=UserSerializer.Detailed.model_validate(request.user))


class UserPassword(SecuredView):
    allowed_while_password_change_required = True

    @openapi.metadata(
        description=(
            "Change the signed-in user's password (#8). Requires the current password; the new one must pass the "
            "configured password validators. Clears `password_change_required`. Accounts from a directory (LDAP) "
            "change their password there and are refused with 409."
        ),
        tags=["Users"],
        summary="Change own password",
    )
    def post(self, request):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        if request.user.is_anonymous:
            raise UnauthorizedException(detail=_("You have to log in!"))

        user = request.user
        if user.auth_source is None or user.auth_source.driver != AuthSource.Driver.DATABASE:
            raise ProblemDetailException(
                _("This account's password is managed by the directory (LDAP); change it there."),
                status=HTTPStatus.CONFLICT,
                detail_type=DetailType.CONFLICT,
            )

        form = PasswordChangeForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)

        if not user.check_password(form.cleaned_data["current_password"]):
            raise ProblemDetailException(
                _("The current password is not correct."),
                status=HTTPStatus.FORBIDDEN,
                detail_type=DetailType.FORBIDDEN,
            )

        new_password = form.cleaned_data["new_password"]
        if new_password == form.cleaned_data["current_password"]:
            raise ProblemDetailException(
                _("The new password must differ from the current one."),
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        try:
            validate_password(new_password, user)
        except DjangoValidationError as error:
            raise ProblemDetailException(
                " ".join(error.messages),
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        user.set_password(new_password)
        user.password_change_required = False
        user.save(update_fields=["password", "password_change_required"])
        return SingleResponse(request, data=UserSerializer.Detailed.model_validate(user))

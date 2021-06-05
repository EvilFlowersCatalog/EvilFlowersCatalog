from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps.api.errors import ValidationException, ProblemDetailException
from apps.api.filters.users import UserFilter
from apps.api.forms.users import UserForm, CreateUserForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.users import UserSerializer
from apps.api.views.base import SecuredView
from apps.core.models import User


class UserManagement(SecuredView):
    def post(self, request):
        form = CreateUserForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if not request.user.is_superuser:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if User.objects.filter(email=form.cleaned_data['email']).exists():
            raise ProblemDetailException(
                request, _("User with same email already exists"), status=HTTPStatus.CONFLICT
            )

        user = User()
        form.fill(user)
        user.set_password(form.cleaned_data['password'])
        user.save()

        return SingleResponse(request, user, serializer=UserSerializer.Base, status=HTTPStatus.CREATED)

    def get(self, request):
        users = UserFilter(request.GET, queryset=User.objects.all(), request=request).qs

        return PaginationResponse(
            request, users, page=request.GET.get('page', 1), serializer=UserSerializer.Base
        )


class UserDetail(SecuredView):
    @staticmethod
    def _get_user(request, user_id: UUID) -> User:
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist as e:
            raise ProblemDetailException(request, _("User not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not request.user.is_superuser and user.id != request.user.id:
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return user

    def get(self, request, user_id: UUID):
        user = self._get_user(request, user_id)

        return SingleResponse(request, user, serializer=UserSerializer.Base)

    def put(self, request, user_id: UUID):
        form = UserForm.create_from_request(request)

        user = self._get_user(request, user_id)

        if not form.is_valid():
            raise ValidationException(request, form)

        form.fill(user)
        if 'password' in form.cleaned_data.keys():
            user.set_password(form.cleaned_data['password'])
        user.save()

        return SingleResponse(request, user, serializer=UserSerializer.Base)

    def delete(self, request, user_id: UUID):
        user = self._get_user(request, user_id)
        user.hard_delete()

        return SingleResponse(request)

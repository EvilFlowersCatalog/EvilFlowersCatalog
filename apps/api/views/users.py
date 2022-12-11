from http import HTTPStatus
from typing import Optional, Callable
from uuid import UUID

from django.utils.translation import gettext as _

from apps.core.errors import ValidationException, ProblemDetailException, UnauthorizedException
from apps.api.filters.users import UserFilter
from apps.api.forms.users import UserForm, CreateUserForm
from apps.api.response import SingleResponse, PaginationResponse
from apps.api.serializers.users import UserSerializer
from apps.core.models import User
from apps.core.views import SecuredView


class UserManagement(SecuredView):
    def post(self, request):
        form = CreateUserForm.create_from_request(request)

        if not form.is_valid():
            raise ValidationException(request, form)

        if not request.user.has_perm('core.add_user'):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        if User.objects.filter(username=form.cleaned_data['username']).exists():
            raise ProblemDetailException(
                request, _("User with same username already exists"), status=HTTPStatus.CONFLICT
            )

        user = User()
        form.populate(user)
        user.set_password(form.cleaned_data['password'])
        user.save()

        return SingleResponse(request, user, serializer=UserSerializer.Base, status=HTTPStatus.CREATED)

    def get(self, request):
        users = UserFilter(request.GET, queryset=User.objects.all(), request=request).qs

        return PaginationResponse(request, users, serializer=UserSerializer.Base)


class UserDetail(SecuredView):
    @staticmethod
    def _get_user(request, user_id: UUID, perm_test: Optional[Callable] = None) -> User:
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist as e:
            raise ProblemDetailException(request, _("User not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not perm_test:
            perm_test = lambda: True

        if not (user.id == request.user.id or perm_test()):
            raise ProblemDetailException(request, _("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return user

    def get(self, request, user_id: UUID):
        user = self._get_user(request, user_id, lambda: request.user.has_perm('core.view_user'))

        return SingleResponse(request, user, serializer=UserSerializer.Base)

    def put(self, request, user_id: UUID):
        form = UserForm.create_from_request(request)

        user = self._get_user(request, user_id, lambda: request.user.has_perm('core.change_user'))

        if not form.is_valid():
            raise ValidationException(request, form)

        form.populate(user)
        if 'password' in form.cleaned_data.keys():
            user.set_password(form.cleaned_data['password'])
        user.save()

        return SingleResponse(request, user, serializer=UserSerializer.Base)

    def delete(self, request, user_id: UUID):
        user = self._get_user(request, user_id, lambda: request.user.has_perm('core.delete_user'))
        user.delete()

        return SingleResponse(request)


class UserMe(SecuredView):
    def get(self, request):
        if request.user.is_anonymous:
            raise UnauthorizedException(request, detail=_('You have to log in!'))

        return SingleResponse(request, request.user, serializer=UserSerializer.Detailed)

from typing import List

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.managers.user import UserManager
from apps.core.models.auth_source import AuthSource

from apps.core.models.base import BaseModel


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    class Meta:
        app_label = 'core'
        db_table = 'users'
        default_permissions = ('add', 'delete', 'change', 'view')

    username = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=30)
    surname = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    auth_source = models.ForeignKey(AuthSource, on_delete=models.CASCADE)

    objects = UserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['name', 'surname']

    @property
    def full_name(self) -> str:
        return f'{self.name} {self.surname}'

    @property
    def permissions(self) -> List[str]:
        return self.get_user_permissions()


__all__ = [
    'User'
]

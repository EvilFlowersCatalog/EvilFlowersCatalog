from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.managers.user import UserManager

from apps.core.models.base import BaseModel


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    class Meta:
        app_label = 'core'
        db_table = 'users'
        default_permissions = ('add', 'delete')

    # Basic info
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=30)
    surname = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    objects = UserManager()
    all_objects = UserManager(alive_only=False)

    USERNAME_FIELD = 'email'
    EMAIL_FIELD = 'email'
    REQUIRED_FIELDS = ['name', 'surname']

    def get_full_name(self) -> str:
        return f'{self.name} {self.surname}'


__all__ = [
    'User'
]

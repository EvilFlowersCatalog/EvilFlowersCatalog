from typing import List
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.managers.user import UserManager
from apps.core.models.auth_source import AuthSource

from apps.core.models.base import BaseModel


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    class Meta:
        app_label = "core"
        db_table = "users"
        default_permissions = ("add", "delete", "change", "view")

    username = models.CharField(max_length=200, unique=True)
    name = models.CharField(max_length=30)
    surname = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    auth_source = models.ForeignKey(AuthSource, on_delete=models.CASCADE)

    # LCP passphrase (stored as SHA-256 hash)
    lcp_passphrase_hash = models.CharField(
        max_length=64, blank=True, null=True, help_text="SHA-256 hash of user's default LCP passphrase"
    )
    lcp_passphrase_hint = models.CharField(
        max_length=255, blank=True, null=True, help_text="Hint to help user remember their LCP passphrase"
    )

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["name", "surname"]

    def save(self, *args, **kwargs):
        # IP-008 Phase 3 C4: LCP server expects uppercase hex for the
        # passphrase hash; normalize on write so the value persisted in
        # the DB matches what `LCPServerClient` sends.
        if self.lcp_passphrase_hash:
            self.lcp_passphrase_hash = self.lcp_passphrase_hash.upper()
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.surname}"

    @property
    def is_staff(self) -> bool:
        return self.is_superuser

    @property
    def permissions(self) -> List[str]:
        return self.get_user_permissions()

    @property
    def catalog_permissions(self) -> dict[UUID, str]:
        result = {}

        for user_catalog in self.user_catalogs.all():
            result[user_catalog.catalog_id] = user_catalog.mode

        return result


__all__ = ["User"]

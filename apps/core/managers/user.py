from django.contrib.auth.base_user import BaseUserManager

from apps.core.managers.base import BaseManager


class UserManager(BaseUserManager, BaseManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        conditions = {
            f"{self.model.USERNAME_FIELD}__iexact": username
        }
        return self.get(**conditions)

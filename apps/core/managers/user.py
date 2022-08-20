from django.contrib.auth.base_user import BaseUserManager

from apps.core.managers.base import BaseManager
from apps.core.models import AuthSource


class UserManager(BaseUserManager, BaseManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        conditions = {
            f"{self.model.USERNAME_FIELD}__iexact": username
        }
        return self.get(**conditions)

    def _create_user(self, username, name, surname, password):
        user = self.model(username=username, name=name, surname=surname)
        user.set_password(password)
        user.auth_source = AuthSource.objects.filter(driver=AuthSource.Driver.DATABASE).order_by('created_at').first()
        return user

    def create_user(self, username, name, surname, password):
        user = self._create_user(username, name, surname, password)
        user.is_superuser = False
        user.save(using=self._db)
        return user

    def create_superuser(self, username, name, surname, password):
        user = self._create_user(username, name, surname, password)
        user.is_superuser = True
        user.save(using=self._db)
        return user

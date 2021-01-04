from django.contrib.auth.base_user import BaseUserManager

from apps.core.managers.base import BaseManager


class UserManager(BaseUserManager, BaseManager):
    use_in_migrations = True

    def get_by_natural_key(self, username):
        conditions = {
            f"{self.model.USERNAME_FIELD}__iexact": username
        }
        return self.get(**conditions)

    def _create_user(self, email, name, surname, password):
        user = self.model(email=email, name=name, surname=surname)
        user.set_password(password)
        return user

    def create_user(self, email, name, surname, password):
        user = self._create_user(email, name, surname, password)
        user.is_superuser = False
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name, surname, password):
        user = self._create_user(email, name, surname, password)
        user.is_superuser = True
        user.save(using=self._db)
        return user

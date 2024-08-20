from object_checker.base_object_checker import AbacChecker

from apps.core.models import (
    User,
    Catalog,
    UserCatalog,
    Entry,
    UserAcquisition,
    ShelfRecord,
)


class CatalogChecker(AbacChecker):
    @staticmethod
    def check_catalog_manage(user: User, obj: Catalog) -> bool:
        if not user.is_authenticated:
            return False

        return obj.user_catalogs.filter(user=user, mode=UserCatalog.Mode.MANAGE).exists()

    @staticmethod
    def check_catalog_write(user: User, obj: Catalog) -> bool:
        if not user.is_authenticated:
            return False

        return obj.user_catalogs.filter(user=user, mode__in=[UserCatalog.Mode.MANAGE, UserCatalog.Mode.WRITE]).exists()

    @staticmethod
    def check_catalog_read(user: User, obj: Catalog) -> bool:
        if user.is_superuser or obj.is_public:
            return True

        return obj.users.contains(user)


class EntryChecker(AbacChecker):
    @staticmethod
    def check_entry_manage(user: User, obj: Entry) -> bool:
        if not user.is_authenticated:
            return False

        if obj.creator_id == user.id:
            return True

        return obj.catalog.user_catalogs.filter(user=user, mode=UserCatalog.Mode.MANAGE).exists()

    @staticmethod
    def check_entry_read(user: User, obj: Entry) -> bool:
        if not user.is_authenticated:
            return False

        return obj.catalog.users.contains(user)


class UserAcquisitionChecker(AbacChecker):
    @staticmethod
    def check_user_acquisition_read(user: User, obj: UserAcquisition):
        if obj.type == UserAcquisition.UserAcquisitionType.SHARED:
            return True

        return obj.user == user


class ShelfRecordChecker(AbacChecker):
    @staticmethod
    def check_shelf_record_access(user: User, obj: ShelfRecord):
        return obj.user == user

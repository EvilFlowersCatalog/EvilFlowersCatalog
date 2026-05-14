from object_checker.base_object_checker import AbacChecker

from apps.core.models import (
    User,
    Catalog,
    UserCatalog,
    Entry,
    UserAcquisition,
    ShelfRecord,
)
from apps.readium.models import License


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

        if not user.is_authenticated:
            return False

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


class LicenseChecker(AbacChecker):
    # IP-004 Phase 4: split the legacy `check_license_manage` predicate so admin
    # operations (state PATCH: return/revoke/cancel) and content downloads
    # (the .lcpl artifact) sit on independent permission axes. Admins may
    # operate state on behalf of users, but must NOT impersonate users to
    # download licensed content. Break-glass for download goes through a
    # separate audited management command, not the regular API.

    @staticmethod
    def check_license_state_manage(user: User, obj: License):
        """Gate license state transitions (PATCH /readium/v1/licenses/{id})."""
        if not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        if obj.user_id == user.id:
            return True

        return obj.entry.catalog.user_catalogs.filter(user=user, mode=UserCatalog.Mode.MANAGE).exists()

    @staticmethod
    def check_license_download(user: User, obj: License):
        """Gate .lcpl download and License Gateway content fetch (owner only)."""
        if not user.is_authenticated:
            return False
        return obj.user_id == user.id

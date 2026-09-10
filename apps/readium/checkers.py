"""
IP-008 Phase 3 D1: License access predicates live on the readium side.

Previously `LicenseChecker` lived in `apps/core/checkers.py`, which forced
`apps.core` to import `apps.readium.models.License`. The directional
inversion makes a future "drop readium" or "swap DRM provider" scenario
painful, and pollutes the core layer with subsystem-specific concepts.

`object-checker`'s `CheckingManager.get_checkers()` discovers subclasses
via `AbacChecker.__subclasses__()`, so as long as this module is
imported before the first permission check (we do it from
`ReadiumConfig.ready()`), the checkers are registered automatically.
"""

from object_checker.base_object_checker import AbacChecker

from apps.core.models import User, UserCatalog
from apps.readium.models import License


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

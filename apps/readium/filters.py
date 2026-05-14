import django_filters
from django.db.models import Q
from django_filters import FilterSet

from apps.core.models import UserCatalog
from apps.readium.models import License, Reservation


class LicenseFilter(FilterSet):
    """
    License filtering system for Readium LCP (Licensed Content Protection) management.

    Provides comprehensive filtering capabilities for digital content licenses,
    including user-based, entry-based, state-based, and time-based filtering.
    Supports license lifecycle management and access control.
    """

    user_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        field_name="user_id",
        label="User UUID",
        help_text="Filter licenses by user UUID. Returns licenses assigned to the specified user.",
    )
    entry_id = django_filters.UUIDFilter(
        lookup_expr="exact",
        field_name="entry_id",
        label="Entry UUID",
        help_text="Filter licenses by entry UUID. Returns licenses for the specified catalog entry or content item.",
    )
    state = django_filters.ChoiceFilter(
        choices=License.LicenseState.choices,
        label="The current state of the license",
        help_text="Filter licenses by their current state. Available states include active, expired, revoked, and pending based on license lifecycle.",
    )
    starts_at__gte = django_filters.IsoDateTimeFilter(
        lookup_expr="gte",
        field_name="starts_at",
        label="Licenses that started after the specific datetime (ISO8601)",
        help_text="Filter licenses that became active on or after the specified ISO8601 datetime. Used for finding recently activated licenses.",
    )
    starts_at__lte = django_filters.IsoDateTimeFilter(
        lookup_expr="lte",
        field_name="starts_at",
        label="Licenses that started before the specific datetime (ISO8601)",
        help_text="Filter licenses that became active on or before the specified ISO8601 datetime. Used for finding older licenses.",
    )
    expires_at__gte = django_filters.IsoDateTimeFilter(
        lookup_expr="gte",
        field_name="expires_at",
        label="Licenses that expire after the specific datetime (ISO8601)",
        help_text="Filter licenses that expire on or after the specified ISO8601 datetime. Used for finding licenses that will remain valid.",
    )
    expires_at__lte = django_filters.IsoDateTimeFilter(
        lookup_expr="lte",
        field_name="expires_at",
        label="Licenses that expire before the specific datetime (ISO8601)",
        help_text="Filter licenses that expire on or before the specified ISO8601 datetime. Used for finding licenses nearing expiration.",
    )
    # IP-003 Phase 2: oversharing discovery — operators filter the regular licenses
    # collection by `device_count__gte=N`. No dedicated /admin/overshared endpoint.
    device_count__gte = django_filters.NumberFilter(
        field_name="device_count",
        lookup_expr="gte",
        label="Licenses with at least this many registered devices",
        help_text="Find potentially overshared licenses. Combine with PATCH state=revoked to revoke.",
    )
    device_count__lte = django_filters.NumberFilter(
        field_name="device_count",
        lookup_expr="lte",
        label="Licenses with at most this many registered devices",
    )

    class Meta:
        model = License
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        # IP-004 Phase 3: catalog managers see licenses for entries in catalogs
        # they MANAGE in addition to their own licenses. Superuser unchanged.
        if not self.request.user.is_superuser:
            managed_catalog_ids = UserCatalog.objects.filter(
                user=self.request.user, mode=UserCatalog.Mode.MANAGE
            ).values_list("catalog_id", flat=True)
            qs = qs.filter(Q(user=self.request.user) | Q(entry__catalog_id__in=managed_catalog_ids))

        return qs


class ReservationFilter(FilterSet):
    """Filter the declarative reservation collection (IP-003 Phase 3)."""

    entry_id = django_filters.UUIDFilter(field_name="entry_id")
    user_id = django_filters.UUIDFilter(field_name="user_id")
    status = django_filters.CharFilter(method="filter_status")

    class Meta:
        model = Reservation
        fields = []

    @classmethod
    def filter_status(cls, qs, name, value):
        if not value:
            return qs
        statuses = [token.strip() for token in str(value).split(",") if token.strip()]
        if not statuses:
            return qs
        return qs.filter(status__in=statuses)

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        # IP-004 Phase 3 / Q4: catalog managers see reservations on entries in
        # catalogs they MANAGE with full row-level data (user_id, position),
        # symmetric with LicenseFilter. Superuser unchanged.
        if not self.request.user.is_superuser:
            managed_catalog_ids = UserCatalog.objects.filter(
                user=self.request.user, mode=UserCatalog.Mode.MANAGE
            ).values_list("catalog_id", flat=True)
            qs = qs.filter(Q(user=self.request.user) | Q(entry__catalog_id__in=managed_catalog_ids))

        return qs

import django_filters
from django_filters import FilterSet

from apps.readium.models import License


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
        field_name="user_id",
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

    class Meta:
        model = License
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user=self.request.user)

        return qs

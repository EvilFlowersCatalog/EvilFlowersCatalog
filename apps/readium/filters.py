import django_filters
from django.db.models import Q
from django.utils import timezone
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
    # A `ready` license is a live loan that has not been opened in a reader
    # yet (LSD registers the first device and flips it to `active`). Clients
    # that filtered "my loans" by `state=active` hid every never-opened loan.
    active = django_filters.BooleanFilter(
        method="filter_active",
        label="Live loans only",
        help_text=(
            "`true` returns loans the user can read right now: state `ready` or `active` and not yet expired. "
            "`false` returns everything else (returned, expired, revoked, cancelled, or lapsed)."
        ),
    )

    class Meta:
        model = License
        fields = []

    @classmethod
    def filter_active(cls, qs, name, value):
        if value is None:
            return qs
        live = Q(state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE], expires_at__gt=timezone.now())
        return qs.filter(live) if value else qs.exclude(live)

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
    """Filter the declarative reservation collection (IP-003 Phase 3).

    Visibility (GitHub #73): the collection is the caller's own queue by
    default. Catalog managers and superusers opt into the wider view with
    `scope=managed`. The previous default (own + every reservation on managed
    catalogs) leaked other users' rows into the portal's "My reservations"
    page for anyone with a manage grant — rendered as duplicate reservations
    of the same title.
    """

    SCOPE_OWN = "own"
    SCOPE_MANAGED = "managed"

    entry_id = django_filters.UUIDFilter(field_name="entry_id")
    user_id = django_filters.UUIDFilter(field_name="user_id")
    status = django_filters.CharFilter(method="filter_status")
    scope = django_filters.ChoiceFilter(
        method="filter_scope",
        choices=((SCOPE_OWN, "Own reservations"), (SCOPE_MANAGED, "Reservations on managed catalogs")),
        help_text=(
            "`own` (default) — only the caller's reservations. `managed` — additionally reservations on "
            "entries in catalogs the caller manages; superusers see every reservation."
        ),
    )

    class Meta:
        model = Reservation
        fields = []

    @classmethod
    def filter_scope(cls, qs, name, value):
        # Applied in `qs` below, where the request user is available.
        return qs

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

        scope = (self.form.cleaned_data.get("scope") if self.is_bound and self.is_valid() else None) or self.SCOPE_OWN
        if scope == self.SCOPE_OWN:
            return qs.filter(user=self.request.user)

        # `managed`: IP-004 Phase 3 / Q4 — catalog managers see reservations on
        # entries in catalogs they MANAGE with full row-level data (user_id,
        # position). Superusers see everything.
        if not self.request.user.is_superuser:
            managed_catalog_ids = UserCatalog.objects.filter(
                user=self.request.user, mode=UserCatalog.Mode.MANAGE
            ).values_list("catalog_id", flat=True)
            qs = qs.filter(Q(user=self.request.user) | Q(entry__catalog_id__in=managed_catalog_ids))

        return qs

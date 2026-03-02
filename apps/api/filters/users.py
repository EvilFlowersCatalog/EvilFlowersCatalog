import django_filters

from apps.core.models import User


class UserFilter(django_filters.FilterSet):
    """
    User filtering system for user management and administration.

    Provides filtering capabilities for user accounts with privacy-aware
    access controls. Supports filtering by identity, activity status,
    and login patterns for user administration.
    """

    id = django_filters.UUIDFilter(
        help_text="Filter users by UUID. Returns the specific user with the given unique identifier."
    )
    username = django_filters.CharFilter(
        lookup_expr="icontains",
        help_text="Filter users by username using case-insensitive partial matching. Searches within user login names.",
    )
    name = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter users by first name using case-insensitive partial matching. Supports Unicode normalization for international names.",
    )
    surname = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter users by surname/last name using case-insensitive partial matching. Supports Unicode normalization for international names.",
    )
    is_active = django_filters.BooleanFilter(
        help_text="Filter users by account status. True returns active users, False returns deactivated accounts."
    )
    last_login_gte = django_filters.DateTimeFilter(
        field_name="last_login",
        lookup_expr="gte",
        help_text="Filter users who logged in on or after the specified datetime. Used for finding recently active users.",
    )
    last_login_lte = django_filters.DateTimeFilter(
        field_name="last_login",
        lookup_expr="lte",
        help_text="Filter users who logged in on or before the specified datetime. Used for finding users with older login activity.",
    )

    class Meta:
        model = User
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.has_perm("core.view_user"):
            qs = qs.filter(pk=self.request.user.id)

        return qs

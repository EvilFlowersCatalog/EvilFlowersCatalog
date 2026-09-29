import django_filters

from apps.core.models import UserActivity


class UserActivityFilter(django_filters.FilterSet):
    """
    Filtering for the authenticated user's personal activity history (IP-016).

    The queryset is always restricted to the requesting user; there is no `user_id` filter.
    """

    action = django_filters.MultipleChoiceFilter(
        choices=UserActivity.ActivityAction.choices,
        help_text="Filter history by action. Repeat the parameter to match several actions "
        "(e.g. `?action=loan_created&action=loan_returned`).",
    )
    entry_id = django_filters.UUIDFilter(
        help_text="Filter history by entry UUID. Returns only activity concerning the specified entry.",
    )
    catalog_id = django_filters.UUIDFilter(
        field_name="entry__catalog_id",
        help_text="Filter history by catalog UUID. Returns activity for entries in the specified catalog.",
    )
    last_occurred_at__gte = django_filters.IsoDateTimeFilter(
        field_name="last_occurred_at",
        lookup_expr="gte",
        help_text="Return activity whose latest occurrence is at or after this ISO 8601 datetime.",
    )
    last_occurred_at__lte = django_filters.IsoDateTimeFilter(
        field_name="last_occurred_at",
        lookup_expr="lte",
        help_text="Return activity whose latest occurrence is at or before this ISO 8601 datetime.",
    )

    class Meta:
        model = UserActivity
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        return qs.filter(user=self.request.user)

import django_filters

from apps.core.models import ApiKey


class ApiKeyFilter(django_filters.FilterSet):
    """
    API key filtering system for authentication token management.
    
    Provides filtering capabilities for API keys - authentication tokens used
    for programmatic access to the system. Supports filtering by user, name,
    and usage patterns.
    """
    
    user_id = django_filters.UUIDFilter(
        help_text="Filter API keys by user UUID. Returns keys belonging to the specified user.",
    )
    name = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter API keys by name using case-insensitive partial matching. Searches within key names and descriptions.",
    )
    last_seen_at__gte = django_filters.DateTimeFilter(
        field_name="last_seen_at",
        lookup_expr="gte",
        help_text="Filter API keys by last usage date. Returns keys used on or after the specified datetime.",
    )
    last_seen_at__lte = django_filters.DateTimeFilter(
        field_name="last_seen_at",
        lookup_expr="lte",
        help_text="Filter API keys by last usage date. Returns keys used on or before the specified datetime.",
    )

    class Meta:
        model = ApiKey
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user=self.request.user)

        return qs

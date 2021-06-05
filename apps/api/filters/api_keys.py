import django_filters

from apps.core.models import ApiKey


class ApiKeyFilter(django_filters.FilterSet):
    user_id = django_filters.UUIDFilter()
    name = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    last_seen_at_gte = django_filters.DateTimeFilter(field_name='last_seen_at', lookup_expr='gte')
    last_seen_at_lte = django_filters.DateTimeFilter(field_name='last_seen_at', lookup_expr='lte')

    class Meta:
        model = ApiKey
        fields = []

    @property
    def qs(self):
        qs = super().qs
        user = getattr(self.request, 'user', None)

        if not user:
            return qs.none()

        if not user.is_superuser:
            qs = qs.filter(user=user)

        return qs

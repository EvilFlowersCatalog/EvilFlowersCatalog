import django_filters

from apps.core.models import ApiKey


class ApiKeyFilter(django_filters.FilterSet):
    user_id = django_filters.UUIDFilter(label="User UUID")
    name = django_filters.CharFilter(lookup_expr="unaccent__icontains", label="API Key Name")
    last_seen_at__gte = django_filters.DateTimeFilter(field_name="last_seen_at", lookup_expr="gte")
    last_seen_at__lte = django_filters.DateTimeFilter(field_name="last_seen_at", lookup_expr="lte")

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

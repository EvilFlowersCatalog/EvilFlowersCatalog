import django_filters

from apps.core.models import User


class UserFilter(django_filters.FilterSet):
    id = django_filters.UUIDFilter()
    email = django_filters.CharFilter(lookup_expr='icontains')
    name = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    surname = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    is_active = django_filters.BooleanFilter()
    last_login_gte = django_filters.DateTimeFilter(field_name='last_login', lookup_expr='gte')
    last_login_lte = django_filters.DateTimeFilter(field_name='last_login', lookup_expr='lte')

    class Meta:
        model = User
        fields = []

    @property
    def qs(self):
        qs = super().qs
        user = getattr(self.request, 'user', None)

        if not user:
            return qs.none()

        if not user.is_superuser:
            qs = qs.filter(pk=user.id)

        return qs

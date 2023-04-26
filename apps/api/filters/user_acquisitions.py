import django_filters

from apps.core.models import UserAcquisition


class UserAcquisitionFilter(django_filters.FilterSet):
    user_id = django_filters.UUIDFilter()
    acquisition_id = django_filters.UUIDFilter()
    type = django_filters.CharFilter(lookup_expr='iexact')
    title = django_filters.CharFilter(lookup_expr='unaccent__icontains', field_name='acquisition__entry__title')
    expire_at_gte = django_filters.DateTimeFilter(field_name='expire_at', lookup_expr='gte')
    expire_at_lte = django_filters.DateTimeFilter(field_name='expire_at', lookup_expr='lte')
    created_at_gte = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_at_lte = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')

    class Meta:
        model = UserAcquisition
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(user=self.request.user)

        return qs

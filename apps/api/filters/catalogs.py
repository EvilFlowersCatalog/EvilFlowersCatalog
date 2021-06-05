import django_filters

from apps.core.models import Catalog


class CatalogFilter(django_filters.FilterSet):
    title = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    url_name = django_filters.CharFilter(lookup_expr='unaccent__icontains')

    class Meta:
        model = Catalog
        fields = []

    @property
    def qs(self):
        qs = super().qs
        user = getattr(self.request, 'user', None)

        if not user:
            return qs.none()

        if not user.is_superuser:
            qs = qs.filter(creator=user)

        return qs

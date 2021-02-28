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
        api_key = getattr(self.request, 'api_key', None)

        if not api_key:
            return qs.none()

        if not api_key.user.is_superuser:
            qs = qs.filter(creator=api_key)

        return qs

import django_filters
from django.db.models import Q

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

        if not self.request.user.is_authenticated:
            return qs.filter(is_public=True)

        if not self.request.user.is_superuser:
            qs = qs.filter(
                Q(users=self.request.user) | Q(is_public=True)
            )

        return qs

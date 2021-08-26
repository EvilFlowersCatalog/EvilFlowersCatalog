import django_filters
from django.db.models import Q

from apps.core.models import Author


class AuthorFilter(django_filters.FilterSet):
    catalog_id = django_filters.UUIDFilter()
    name = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    surname = django_filters.CharFilter(lookup_expr='unaccent__icontains')
    query = django_filters.CharFilter(method='filter_query')

    class Meta:
        model = Author
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(
            Q(name__unaccent__icontains=value) |
            Q(surname__unaccent__icontains=value)
        )

    @property
    def qs(self):
        qs = super().qs
        user = getattr(self.request, 'user', None)

        if not user:
            return qs.none()

        if not user.is_superuser:
            qs = qs.filter(catalog__creator=user)

        return qs

import django_filters
from django.db.models import Value, CharField, Q
from django.db.models.functions import Concat

from apps.core.models import Author


class AuthorFilter(django_filters.FilterSet):
    catalog_id = django_filters.UUIDFilter()
    name = django_filters.CharFilter(lookup_expr="unaccent__icontains")
    surname = django_filters.CharFilter(lookup_expr="unaccent__icontains")
    query = django_filters.CharFilter(method="filter_query")

    class Meta:
        model = Author
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return (
            qs.annotate(search_query=Concat("name", Value(" "), "surname", output_field=CharField()))
            .filter(search_query__unaccent__icontains=value)
            .distinct()
        )

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.filter(catalog__is_public=True)

        if not self.request.user.is_superuser:
            qs = qs.filter(Q(catalog__users=self.request.user) | Q(catalog__is_public=True))

        return qs

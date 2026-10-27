import django_filters
from django.db.models import Q

from apps.core.models import Language


class LanguageFilter(django_filters.FilterSet):
    """
    Language filtering for the ISO 639-1 language list.
    """

    translatable = django_filters.BooleanFilter(
        help_text="Filter languages offered for translating feed titles and category labels (configured by administrators).",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Search languages by name or ISO 639-1 / 639-2 code.",
    )

    class Meta:
        model = Language
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(Q(name__unaccent__icontains=value) | Q(alpha2__iexact=value) | Q(alpha3__iexact=value))

import django_filters
from django.db.models import Value, CharField, Q
from django.db.models.functions import Concat

from apps.core.models import Category


class CategoryFilter(django_filters.FilterSet):
    """
    Category filtering system for content organization and discovery.

    Provides filtering capabilities for categories used to classify and organize
    catalog entries. Supports both exact term matching and flexible label-based
    searching with schema-aware filtering.
    """

    catalog_id = django_filters.UUIDFilter(
        help_text="Filter categories by catalog UUID. Returns only categories that exist within the specified catalog."
    )
    term = django_filters.CharFilter(
        lookup_expr="unaccent__iexact",
        help_text="Filter categories by exact term match. Terms are the unique identifiers for categories, typically using standardized vocabulary (e.g., 'fiction', 'science').",
    )
    label = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter categories by label using case-insensitive partial matching. Labels are human-readable descriptions of category terms.",
    )
    scheme = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter categories by classification scheme using partial matching. Schemes indicate the vocabulary or taxonomy used (e.g., 'http://www.bisg.org/standards/bisac_subject/index.html').",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Perform full-text search across category terms and labels. Searches both the technical term and human-readable label for comprehensive category discovery.",
    )

    class Meta:
        model = Category
        fields = []

    @staticmethod
    def filter_query(qs, name, value):
        return (
            qs.annotate(search_query=Concat("term", Value(";"), "label", output_field=CharField()))
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

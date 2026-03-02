import django_filters
from django.db.models import Value, CharField, Q
from django.db.models.functions import Concat

from apps.core.models import Author
from apps.api.filters.base import BaseSecuredFilter


class AuthorFilter(BaseSecuredFilter):
    """
    Author filtering system for finding and browsing content creators.

    Provides comprehensive search capabilities for authors including name-based
    filtering, catalog-specific searches, and intelligent full-text search
    across author metadata.
    """

    catalog_id = django_filters.UUIDFilter(
        help_text="Filter authors by catalog UUID. Returns only authors who have created content in the specified catalog."
    )
    name = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter authors by first name using case-insensitive partial matching. Supports Unicode normalization for international names.",
    )
    surname = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter authors by surname/last name using case-insensitive partial matching. Supports Unicode normalization for international names.",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Perform full-text search across author names. Searches both individual name fields and combined full names for comprehensive author discovery.",
    )

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
        # Use cached access control from base class
        return self.apply_related_catalog_access_control(qs, "catalog")

import django_filters
from django.db.models import Q

from apps.core.models import Catalog


class CatalogFilter(django_filters.FilterSet):
    """
    Catalog filtering system for browsing and discovering content collections.

    Provides filtering capabilities for catalogs - the main organizational units
    that contain collections of entries. Supports both title-based and URL-based
    filtering with proper access control.
    """

    title = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter catalogs by title using case-insensitive partial matching. Searches within catalog names and descriptions.",
    )
    url_name = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter catalogs by URL name using partial matching. URL names are the unique identifiers used in catalog URLs (e.g., 'my-catalog' in '/catalogs/my-catalog/').",
    )

    class Meta:
        model = Catalog
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.filter(is_public=True)

        if not self.request.user.is_superuser:
            qs = qs.filter(Q(users=self.request.user) | Q(is_public=True))

        return qs

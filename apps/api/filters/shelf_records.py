import django_filters
from django.db.models import Q
from django.utils.translation import gettext as _

from apps.core.models import ShelfRecord


class ShelfRecordFilter(django_filters.FilterSet):
    """
    Shelf record filtering system for user reading collections and bookmarks.

    Provides filtering capabilities for shelf records - user's personal collections
    of entries including reading lists, favorites, and bookmarks. Supports filtering
    by user, entry metadata, and content characteristics.
    """

    catalog_id = django_filters.UUIDFilter(
        field_name="entry__catalog_id",
        help_text="Filter shelf records by catalog UUID. Returns shelf records for entries in the specified catalog.",
    )
    catalog_title = django_filters.CharFilter(
        field_name="entry__catalog__title",
        lookup_expr="unaccent__icontains",
        help_text="Filter shelf records by catalog title using case-insensitive partial matching.",
    )
    entry_id = django_filters.UUIDFilter(
        help_text="Filter shelf records by entry UUID. Returns shelf records for the specified catalog entry.",
    )
    user_id = django_filters.UUIDFilter(
        help_text="Filter shelf records by user UUID. Returns shelf records belonging to the specified user.",
    )
    author_id = django_filters.UUIDFilter(
        method="filter_author_id",
        help_text="Filter shelf records by author UUID. Returns shelf records for entries by the specified author.",
    )
    author = django_filters.CharFilter(
        method="filter_author",
        help_text="Filter shelf records by author name using case-insensitive partial matching. Searches author names and surnames.",
    )
    category_id = django_filters.UUIDFilter(
        field_name="entry__categories__id",
        help_text="Filter shelf records by category UUID. Returns shelf records for entries in the specified category.",
    )
    category_term = django_filters.CharFilter(
        field_name="entry__categories__term",
        help_text="Filter shelf records by category term using exact matching. Categories help organize content by subject or genre.",
    )
    language_id = django_filters.UUIDFilter(
        help_text="Filter shelf records by language UUID. Returns shelf records for entries in the specified language.",
    )
    language_code = django_filters.CharFilter(
        field_name="entry__language__code",
        help_text="Filter shelf records by ISO language code (e.g., 'en', 'es', 'fr'). Returns shelf records for entries in the specified language.",
    )
    title = django_filters.CharFilter(
        field_name="entry__title",
        lookup_expr="unaccent__icontains",
        help_text="Filter shelf records by entry title using case-insensitive partial matching. Searches within entry titles.",
    )
    summary = django_filters.CharFilter(
        field_name="entry__summary",
        lookup_expr="unaccent__icontains",
        help_text="Filter shelf records by entry summary using case-insensitive partial matching. Searches within entry descriptions.",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Perform full-text search across entry titles and summaries in shelf records. Useful for finding specific content in user collections.",
    )
    feed_id = django_filters.UUIDFilter(
        field_name="entry__feeds__id",
        help_text="Filter shelf records by feed UUID. Returns shelf records for entries belonging to the specified feed.",
    )

    class Meta:
        model = ShelfRecord
        fields = []

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.none()

        if not self.request.user.is_superuser:
            qs = qs.filter(Q(entry__catalog__users=self.request.user) & Q(user=self.request.user))

        return qs

    @staticmethod
    def filter_author(qs, name, value):
        return qs.filter(
            Q(entry__authors__name__unaccent__icontains=value) | Q(entry__authors__surname__unaccent__icontains=value)
        )

    @staticmethod
    def filter_author_id(qs, name, value):
        return qs.filter(entry__authors__id=value)

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(Q(entry__title__unaccent__icontains=value) | Q(entry__summary__unaccent__icontains=value))

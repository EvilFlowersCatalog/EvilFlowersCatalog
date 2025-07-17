import uuid

import django_filters
from django.core.exceptions import ValidationError
from django.forms import UUIDField

from apps.core.models import Feed


class FeedFilter(django_filters.FilterSet):
    """
    Feed filtering system for organizing and browsing content collections.

    Provides filtering capabilities for feeds - structured collections of entries
    that can be organized hierarchically. Supports filtering by creator, catalog,
    type, and hierarchical relationships.
    """

    creator_id = django_filters.UUIDFilter(
        help_text="Filter feeds by creator UUID. Returns feeds created by the specified user."
    )
    catalog_id = django_filters.UUIDFilter(
        help_text="Filter feeds by catalog UUID. Returns only feeds belonging to the specified catalog."
    )
    title = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter feeds by title using case-insensitive partial matching. Searches within feed names and descriptions.",
    )
    kind = django_filters.ChoiceFilter(
        choices=Feed.FeedKind.choices,
        help_text="Filter feeds by type/kind. Available types include navigation feeds, acquisition feeds, and other specialized feed types for different content organization patterns.",
    )
    parent_id = django_filters.CharFilter(
        method="filter_parent_id",
        help_text="Filter feeds by parent relationship. Use 'null' to find top-level feeds, or provide a UUID to find child feeds of a specific parent.",
    )

    class Meta:
        model = Feed
        fields = []

    @staticmethod
    def filter_parent_id(qs, name, value):
        # TODO: this should be a convention
        if value == "null":
            return qs.filter(parents__isnull=True).distinct()
        else:
            try:
                value = uuid.UUID(value)
            except ValueError:
                raise ValidationError(UUIDField.default_error_messages["invalid"], code="invalid")
            return qs.filter(parents__id=value).distinct()

    @property
    def qs(self):
        qs = super().qs

        if not self.request.user.is_authenticated:
            return qs.filter(catalog__is_public=True)

        if not self.request.user.is_superuser:
            # TODO: catalog access mode should be an integer so we can do gte a lte
            qs = qs.filter(
                catalog__user_catalogs__user=self.request.user,
                # catalog__user_catalogs__mode=UserCatalog.Mode.READ
            )

        return qs

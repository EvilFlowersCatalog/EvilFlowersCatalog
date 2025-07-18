from typing import List
from urllib.parse import urlencode

import django_filters
from django.core.exceptions import ValidationError
from django.db.models import Q, Value, CharField, Case, When, IntegerField
from django.db.models.functions import Concat, Lower
from django.utils.translation import gettext as _
from partial_date import PartialDate

from apps.core.models import Entry, Language, Category, Author
from apps.opds.structures import Facet
from apps.api.filters.base import BaseSecuredFilter


class EntryFilter(BaseSecuredFilter):
    """
    Comprehensive filtering system for catalog entries with advanced search capabilities.

    Provides multiple filtering options including full-text search, metadata filtering,
    and author/category faceting. Supports OpenSearch template compatibility for
    standardized catalog browsing and discovery.
    """

    class Meta:
        model = Entry
        fields = []

    # OpenSearch template configuration
    # TODO: Implement proper OpenSearch template mapping for better API compatibility

    creator_id = django_filters.UUIDFilter(
        help_text="Filter entries by the UUID of the user who created them. Useful for finding entries added by specific contributors."
    )
    catalog_id = django_filters.UUIDFilter(
        help_text="Filter entries by catalog UUID. Returns only entries belonging to the specified catalog."
    )
    catalog_title = django_filters.CharFilter(
        field_name="catalog__title",
        lookup_expr="unaccent__icontains",
        help_text="Filter entries by catalog title using case-insensitive partial matching. Supports Unicode normalization.",
    )
    author_id = django_filters.UUIDFilter(
        method="filter_author_id",
        label=_("Author"),
        help_text="Filter entries by author UUID. Returns entries written by the specified author.",
    )
    author = django_filters.CharFilter(
        method="filter_author",
        help_text="Filter entries by author name using intelligent search. Searches across author names, surnames, and combined full names with partial matching.",
    )
    category_id = django_filters.UUIDFilter(
        label=_("Category"),
        field_name="categories__id",
        help_text="Filter entries by category UUID. Returns entries tagged with the specified category.",
    )
    category_term = django_filters.CharFilter(
        field_name="categories__term",
        help_text="Filter entries by category term using exact matching. Categories help organize content by subject, genre, or classification.",
    )
    language_id = django_filters.UUIDFilter(
        help_text="Filter entries by language UUID. Returns entries in the specified language."
    )
    language_code = django_filters.CharFilter(
        method="filter_language_code",
        label=_("Language"),
        help_text="Filter entries by ISO language code (e.g., 'en', 'es', 'fr'). Accepts both 2-letter (alpha2) and 3-letter (alpha3) ISO codes.",
    )
    title = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter entries by title using case-insensitive partial matching. Supports Unicode normalization for international characters.",
    )
    summary = django_filters.CharFilter(
        lookup_expr="unaccent__icontains",
        help_text="Filter entries by summary/description content using case-insensitive partial matching. Searches within entry descriptions and abstracts.",
    )
    query = django_filters.CharFilter(
        method="filter_query",
        help_text="Perform comprehensive full-text search across all entry fields including title, summary, content, author names, categories, and publisher. Results are ranked by relevance with title matches having highest priority.",
    )
    feed_id = django_filters.UUIDFilter(
        field_name="feeds__id",
        help_text="Filter entries by feed UUID. Returns entries that belong to the specified feed or collection.",
    )
    published_at__gte = django_filters.CharFilter(
        method="filter_published_at_gte",
        help_text="Filter entries published on or after the specified date. Accepts partial dates (YYYY, YYYY-MM, YYYY-MM-DD) and full ISO dates.",
    )
    published_at__lte = django_filters.CharFilter(
        method="filter_published_at_lte",
        help_text="Filter entries published on or before the specified date. Accepts partial dates (YYYY, YYYY-MM, YYYY-MM-DD) and full ISO dates.",
    )
    config__readium_enabled = django_filters.BooleanFilter(
        field_name="config__readium_enabled",
        help_text="Filter entries by Readium LCP (Licensed Content Protection) availability. True returns only DRM-protected entries, False returns unprotected entries.",
    )

    @classmethod
    def template(cls) -> str:
        params = {}
        for key, definition in cls.base_filters.items():
            params[key] = definition.extra.get("value", key)

        return urlencode(params)

    @property
    def facets(self) -> List[Facet]:
        facets = []

        # Language
        available_languages = Language.objects.filter(entries__in=self.qs).distinct()
        for language in available_languages:
            url_params = self.request.GET.dict()
            url_params["language_code"] = language.alpha2
            facets.append(
                Facet(
                    title=language.name,
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Language"),
                    count=self.qs.filter(language=language).count(),
                    is_active=self.request.GET.get("language_code") == language.alpha2,
                )
            )

        # Categories
        available_categories = Category.objects.filter(entries__in=self.qs).distinct()
        for category in available_categories:
            url_params = self.request.GET.dict()
            url_params["category_id"] = category.id
            facets.append(
                Facet(
                    title=category.label or category.term,
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Category"),
                    count=self.qs.filter(categories=category).count(),
                    is_active=self.request.GET.get("category_id") == category.id,
                )
            )

        # Authors
        available_authors = Author.objects.filter(entries__in=self.qs).distinct()
        for author in available_authors:
            url_params = self.request.GET.dict()
            url_params["author_id"] = author.id
            facets.append(
                Facet(
                    title=author.full_name,
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Author"),
                    count=self.qs.filter(authors=author).distinct().count(),
                    is_active=self.request.GET.get("author_id") == author.id,
                )
            )

        return facets

    @property
    def qs(self):
        qs = super().qs
        # Use cached access control from base class
        return self.apply_catalog_access_control(qs)

    @staticmethod
    def filter_author(qs, name, value):
        return (
            qs.annotate(
                author_full_name=Concat("authors__name", Value(" "), "authors__surname", output_field=CharField())
            )
            .filter(
                Q(author_full_name__unaccent__icontains=value)
                | Q(authors__name__unaccent__icontains=value)
                | Q(authors__surname__unaccent__icontains=value)
            )
            .distinct()
        )

    @staticmethod
    def filter_author_id(qs, name, value):
        return qs.filter(authors__id=value)

    @staticmethod
    def filter_query(qs, name, value):
        # Optimized search implementation
        search_terms = value.strip().split()

        # Use simpler query structure for better performance
        base_conditions = []

        # Create efficient OR conditions for each term
        for term in search_terms:
            term_conditions = (
                Q(title__icontains=term)
                | Q(summary__icontains=term)
                | Q(publisher__icontains=term)
                | Q(authors__name__icontains=term)
                | Q(authors__surname__icontains=term)
                | Q(categories__label__icontains=term)
                | Q(categories__term__icontains=term)
            )
            base_conditions.append(term_conditions)

        # Combine all conditions with AND (all terms must match somewhere)
        if base_conditions:
            combined_query = base_conditions[0]
            for condition in base_conditions[1:]:
                combined_query &= condition
        else:
            combined_query = Q()

        # Apply filtering with simpler relevance scoring
        return (
            qs.filter(combined_query)
            .annotate(
                # Simplified relevance scoring based on field priority
                relevance_score=Case(
                    When(title__icontains=value, then=Value(100)),
                    When(authors__name__icontains=value, then=Value(90)),
                    When(authors__surname__icontains=value, then=Value(90)),
                    When(publisher__icontains=value, then=Value(80)),
                    When(categories__label__icontains=value, then=Value(70)),
                    When(summary__icontains=value, then=Value(60)),
                    default=Value(50),
                    output_field=IntegerField(),
                )
            )
            .distinct()
            .order_by("-relevance_score", "-popularity", "-created_at")
        )

    @staticmethod
    def filter_published_at_gte(qs, name, value):
        try:
            value = PartialDate(value)
            return qs.filter(published_at__gte=value)
        except ValidationError:
            return qs

    @staticmethod
    def filter_published_at_lte(qs, name, value):
        try:
            value = PartialDate(value)
            return qs.filter(published_at__lte=value)
        except ValidationError:
            return qs

    @staticmethod
    def filter_language_code(qs, name, value):
        """Filter entries by language code, checking both alpha2 and alpha3 fields."""
        return qs.filter(Q(language__alpha2=value) | Q(language__alpha3=value))

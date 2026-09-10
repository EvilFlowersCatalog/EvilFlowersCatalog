from typing import List
from urllib.parse import urlencode

import django_filters
from django.core.exceptions import ValidationError
from django.db.models import Q, Value, CharField, Case, When, IntegerField, Count
from django.db.models.functions import Concat, Lower
from django.utils.translation import gettext as _
from partial_date import PartialDate

from apps.core.models import Entry
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

    id = django_filters.CharFilter(
        method="filter_id",
        label=_("Entry IDs"),
        help_text=(
            "Filter entries by UUID. Accepts a single UUID or a comma-separated list "
            "(e.g. `?id=<uuid>,<uuid>`). Lets clients resolve many entries in one request "
            "instead of issuing one detail call per id."
        ),
    )
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
    author_id = django_filters.CharFilter(
        method="filter_author_id",
        label=_("Author"),
        help_text=(
            "Filter entries by author UUID. Accepts a single UUID or a comma-separated list of UUIDs "
            "(e.g. `?author_id=<uuid>,<uuid>`). Multi-value form expands to an OR/IN lookup."
        ),
    )
    author = django_filters.CharFilter(
        method="filter_author",
        help_text="Filter entries by author name using intelligent search. Searches across author names, surnames, and combined full names with partial matching.",
    )
    category_id = django_filters.CharFilter(
        method="filter_category_id",
        label=_("Category"),
        help_text=(
            "Filter entries by category UUID. Accepts a single UUID or a comma-separated list "
            "(e.g. `?category_id=<uuid>,<uuid>`). Multi-value form expands to an OR/IN lookup."
        ),
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
        help_text=(
            "Filter entries by ISO language code (e.g., 'en', 'es', 'fr'). Accepts both 2-letter (alpha2) "
            "and 3-letter (alpha3) ISO codes, as a single value or comma-separated list "
            "(e.g. `?language_code=sk,en`)."
        ),
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
    feed_id = django_filters.CharFilter(
        method="filter_feed_id",
        help_text=(
            "Filter entries by feed UUID. Accepts a single UUID or a comma-separated list "
            "(e.g. `?feed_id=<uuid>,<uuid>`). Multi-value form expands to an OR/IN lookup."
        ),
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
    # IP-004 Phase 5: saturation filters. Values are computed per-request by
    # `lcp_state_mapping(request.user, entries)` and post-filtered in Python.
    # Intended for paginated admin views; linear in catalog size.
    lcp_state = django_filters.CharFilter(
        method="filter_lcp_state",
        help_text=(
            "Filter entries by computed LCP availability state. Accepts a single value or "
            "a comma-separated list of: `not_lcp`, `available_now`, `available_in_days`, "
            "`active_loan_for_user`, `fully_borrowed` (e.g. `?lcp_state=fully_borrowed,available_in_days`)."
        ),
    )
    over_saturated = django_filters.BooleanFilter(
        method="filter_over_saturated",
        help_text=(
            "Filter entries by whether their active license count exceeds their "
            "configured `readium_amount`. `true` surfaces legacy over-saturated entries "
            "that need manual remediation. `false` returns entries within their cap."
        ),
    )

    @classmethod
    def template(cls) -> str:
        params = {}
        for key, definition in cls.base_filters.items():
            params[key] = definition.extra.get("value", key)

        return urlencode(params)

    @property
    def facets(self) -> List[Facet]:
        # Evaluate the access-controlled queryset once (the `qs` property
        # re-applies access control on every access) and compute each
        # dimension's counts with a single grouped aggregate instead of one
        # COUNT query per language / category / author.
        qs = self.qs
        facets = []

        # Language — one grouped COUNT over all present languages.
        language_rows = (
            qs.filter(language__isnull=False)
            .values("language__name", "language__alpha2")
            .annotate(count=Count("id", distinct=True))
        )
        for row in language_rows:
            alpha2 = row["language__alpha2"]
            url_params = self.request.GET.dict()
            url_params["language_code"] = alpha2
            facets.append(
                Facet(
                    title=row["language__name"],
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Language"),
                    count=row["count"],
                    is_active=self.request.GET.get("language_code") == alpha2,
                )
            )

        # Categories — one grouped COUNT over all present categories.
        category_rows = (
            qs.filter(categories__isnull=False)
            .values("categories__id", "categories__term", "categories__label")
            .annotate(count=Count("id", distinct=True))
        )
        for row in category_rows:
            category_id = row["categories__id"]
            url_params = self.request.GET.dict()
            url_params["category_id"] = category_id
            facets.append(
                Facet(
                    title=row["categories__label"] or row["categories__term"],
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Category"),
                    count=row["count"],
                    is_active=self.request.GET.get("category_id") == str(category_id),
                )
            )

        # Authors — one grouped COUNT over all present authors.
        author_rows = (
            qs.filter(authors__isnull=False)
            .values("authors__id", "authors__name", "authors__surname")
            .annotate(count=Count("id", distinct=True))
        )
        for row in author_rows:
            author_id = row["authors__id"]
            full_name = f"{row['authors__name']} {row['authors__surname']}".strip()
            url_params = self.request.GET.dict()
            url_params["author_id"] = author_id
            facets.append(
                Facet(
                    title=full_name,
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Author"),
                    count=row["count"],
                    is_active=self.request.GET.get("author_id") == str(author_id),
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
    def _split_csv(value):
        """Split a comma-separated filter value into a list of stripped, non-empty tokens."""
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [token.strip() for token in str(value).split(",") if token.strip()]

    @classmethod
    def filter_id(cls, qs, name, value):
        ids = cls._split_csv(value)
        if not ids:
            return qs
        return qs.filter(id__in=ids)

    @classmethod
    def filter_author_id(cls, qs, name, value):
        ids = cls._split_csv(value)
        if not ids:
            return qs
        return qs.filter(authors__id__in=ids).distinct()

    @classmethod
    def filter_category_id(cls, qs, name, value):
        ids = cls._split_csv(value)
        if not ids:
            return qs
        return qs.filter(categories__id__in=ids).distinct()

    @classmethod
    def filter_feed_id(cls, qs, name, value):
        ids = cls._split_csv(value)
        if not ids:
            return qs
        return qs.filter(feeds__id__in=ids).distinct()

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

    @classmethod
    def filter_language_code(cls, qs, name, value):
        """Filter entries by language code(s); accepts comma-separated alpha2/alpha3 codes."""
        codes = cls._split_csv(value)
        if not codes:
            return qs
        return qs.filter(Q(language__alpha2__in=codes) | Q(language__alpha3__in=codes))

    # IP-004 Phase 5: saturation post-filters. These are instance methods so they
    # have access to `self.request.user`, which `lcp_state_mapping` needs to
    # compute `active_loan_for_user` correctly.

    def filter_lcp_state(self, qs, name, value):
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        states = {token for token in self._split_csv(value)}
        if not states:
            return qs

        user = self.request.user if self.request is not None else None
        materialized = list(qs)
        mapping = lcp_state_mapping(user, materialized)
        matching_ids = [
            entry.pk
            for entry in materialized
            if (row := mapping.get(entry.pk)) is not None
            and str(row["lcp_state"].value if hasattr(row["lcp_state"], "value") else row["lcp_state"]) in states
        ]
        return qs.filter(pk__in=matching_ids)

    def filter_over_saturated(self, qs, name, value):
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        if value is None:
            return qs

        user = self.request.user if self.request is not None else None
        materialized = list(qs)
        mapping = lcp_state_mapping(user, materialized)
        matching_ids = [
            entry.pk
            for entry in materialized
            if (row := mapping.get(entry.pk)) is not None and bool(row.get("over_saturated")) is bool(value)
        ]
        return qs.filter(pk__in=matching_ids)

import re
from typing import Optional

import django_filters
from django.core.exceptions import ValidationError
from django.db.models import Q, Value, CharField, Exists, OuterRef
from django.db.models.functions import Concat
from django.utils.translation import gettext as _
from partial_date import PartialDate

from apps.core.models import Acquisition, Entry
from apps.core.search.query import Channel, Level, SearchState, did_you_mean
from apps.api.filters.base import BaseSecuredFilter

# OpenSearch 1.1 parameters in the `ef` namespace (declared by the OPDS 1.2 search descriptor).
OPENSEARCH_NAMESPACE = "ef"


def fulltext_runs():
    """Extraction runs that produced the full text of an acquisition."""
    from apps.extraction.models import ExtractionRun

    return ExtractionRun.objects.filter(status=ExtractionRun.Status.DONE)


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
        help_text=(
            "Full-text search over title, author names, category terms and labels (all translations), "
            "publisher and summary. Words are stemmed for the entry's language and matched with and without "
            'diacritics; web-search syntax works (`"exact phrase"`, `-excluded`, `a or b`). Results are '
            "ranked by relevance (title > authors/categories > publisher/summary). When no entry contains "
            "every word, entries with any word are returned, then entries with a similar title or author "
            "(typos); `GET /api/v1/entries` reports this in `metadata.search`."
        ),
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
            "`active_loan_for_user`, `fully_borrowed` (e.g. `?lcp_state=fully_borrowed,available_in_days`). "
            "The pseudo-state `reserved` matches entries the requesting user holds a queued or "
            "ready reservation on; values are OR-ed."
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

    extraction_status = django_filters.CharFilter(
        method="filter_extraction_status",
        help_text=(
            "Filter entries by their AI extraction (IP-018): the status of the newest run, a single value or "
            "a comma-separated list of `unsupported` (nothing to process), `none` (never queued), `queued`, "
            "`running`, `done`, `failed`. The pseudo-value `available` matches entries with an active run, "
            "whatever a newer run is doing."
        ),
    )

    publisher = django_filters.CharFilter(
        method="filter_publisher",
        help_text=(
            "Filter entries by publisher (case-insensitive exact match). Accepts a comma-separated list; "
            "values are OR-ed. Matches the values of the `publishers` facet."
        ),
    )
    format = django_filters.CharFilter(
        method="filter_format",
        help_text=(
            "Filter entries by acquisition media type (e.g. `application/pdf`, `application/epub+zip`). "
            "Accepts a comma-separated list; values are OR-ed."
        ),
    )
    fulltext = django_filters.BooleanFilter(
        method="filter_fulltext",
        help_text=(
            "`true` returns entries whose full text has been extracted (a finished extraction run), "
            "`false` entries without one."
        ),
    )

    # An OpenSearch client that does not fill an optional template parameter may send it back
    # verbatim (`?category_id={ef:categoryId?}`); such a value means "not set".
    UNFILLED_TEMPLATE = re.compile(r"^\{[^{}]*\}$")

    # Filters a reader may fill from the OpenSearch descriptor: parameter -> template name, all in the
    # `ef` namespace so no client fills them on its own (the core `{language}` defaults to `*` or the
    # reader's UI locale, which would silently filter results). The trailing `?` marks them optional.
    OPENSEARCH_TEMPLATE = {
        "language_code": f"{OPENSEARCH_NAMESPACE}:language_code?",
        "category_id": f"{OPENSEARCH_NAMESPACE}:category_id?",
        "author_id": f"{OPENSEARCH_NAMESPACE}:author_id?",
        "feed_id": f"{OPENSEARCH_NAMESPACE}:feed_id?",
        "published_at__gte": f"{OPENSEARCH_NAMESPACE}:published_at__gte?",
        "published_at__lte": f"{OPENSEARCH_NAMESPACE}:published_at__lte?",
    }

    def __init__(
        self,
        data=None,
        *args,
        search: Optional[SearchState] = None,
        search_channels: tuple = Channel.BOTH,
        **kwargs,
    ):
        """
        `search`: an already resolved free-text search (facets reuse the level of the main result
        instead of resolving the fallback again).
        `search_channels`: which channels a new search uses (OPDS `mode=keyword` / `mode=semantic`).
        """
        self.search_channels = search_channels
        if data is not None and hasattr(data, "lists"):
            unfilled = [key for key, values in data.lists() if values and all(self._unfilled(v) for v in values)]
            if unfilled:
                data = data.copy()
                for key in unfilled:
                    data.pop(key)
        super().__init__(data, *args, **kwargs)
        #: Set by `filter_query`: the free-text search of this request (level, suggestion, ...).
        self.search: Optional[SearchState] = search
        self._search_resolved = search is not None
        # A search handed in (facets) only re-applies the main result's; it records nothing.
        self._search_given = search is not None

    @classmethod
    def _unfilled(cls, value) -> bool:
        return isinstance(value, str) and bool(cls.UNFILLED_TEMPLATE.match(value.strip()))

    @property
    def qs(self):
        if not hasattr(self, "_secured_qs"):
            qs = self.apply_catalog_access_control(super().qs)
            if self.search is not None and not self._search_resolved:
                self._search_resolved = True
                if self._needs_fallback(qs):
                    qs = self._search_fallback(qs)
                # Filters after `query` may have removed some hits: keep what the result holds.
                search = self.search
                search.lexical_ids = set(qs.filter(pk__in=list(search.lexical_ids)).values_list("pk", flat=True))
                search.vector_ids = set(qs.filter(pk__in=list(search.vector_ids)).values_list("pk", flat=True))
            self._secured_qs = qs
        return self._secured_qs

    def _needs_fallback(self, qs) -> bool:
        """No entry has every word. Semantic neighbours alone do not stop the lexical fallback."""
        if Channel.LEXICAL not in self.search.channels:
            return False
        return not qs.filter(pk__in=list(self.search.lexical_ids)).exists()

    def _search_fallback(self, strict_qs):
        """Retry the words with any word, then with trigram similarity (typos); keep the first with hits."""
        strict_ids = (set(self.search.lexical_ids), set(self.search.vector_ids))
        qs = strict_qs
        for level in (Level.RELAXED, Level.FUZZY):
            self.search.level = level
            del self._qs  # django-filter's cache of the filtered queryset
            candidate = self.apply_catalog_access_control(super().qs)
            if not self._needs_fallback(candidate):
                qs = candidate
                break
        else:
            # Nothing lexical at any level: the strict result (semantic neighbours, or nothing).
            self.search.level = Level.STRICT
            self.search.lexical_ids, self.search.vector_ids = strict_ids

        readable = self.apply_catalog_access_control(self.queryset)
        self.search.did_you_mean = did_you_mean(self.search.text, readable)
        return qs

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

    def filter_query(self, qs, name, value):
        """
        Hybrid search (IP-020, IP-021): the words over `entries.search_vector` (title A, authors and
        categories B, publisher and summary C), ranked by BM25 when pg_search is installed, fused with the
        entries closest in meaning (TEI vectors) by Reciprocal Rank Fusion. Annotates `relevance_score`;
        when nothing matches every word, `qs` falls back to any word, then to similar titles/authors
        (see `self.search`).
        """
        text = (value or "").strip()
        if not text:
            return qs
        if self.search is None:
            self.search = SearchState(text, channels=self.search_channels)
        # Ranked among the entries the caller may read, so the vector channel's top-N is never spent
        # on another catalog's entries.
        qs = self.apply_catalog_access_control(qs)
        return self.search.apply(qs, record=not self._search_given).order_by("-relevance_score", "-popularity", "-pk")

    @classmethod
    def filter_publisher(cls, qs, name, value):
        publishers = cls._split_csv(value)
        if not publishers:
            return qs
        condition = Q()
        for publisher in publishers:
            condition |= Q(publisher__iexact=publisher)
        return qs.filter(condition)

    @classmethod
    def filter_format(cls, qs, name, value):
        mimes = cls._split_csv(value)
        if not mimes:
            return qs
        return qs.filter(Exists(Acquisition.objects.filter(entry_id=OuterRef("pk"), mime__in=mimes)))

    @staticmethod
    def filter_fulltext(qs, name, value):
        if value is None:
            return qs
        has_text = Exists(fulltext_runs().filter(acquisition__entry_id=OuterRef("pk")))
        return qs.filter(has_text if value else ~has_text)

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
        """Filter entries by language code(s); accepts comma-separated alpha2/alpha3 codes. `*` means any."""
        codes = cls._split_csv(value)
        if not codes or "*" in codes:
            return qs
        return qs.filter(Q(language__alpha2__in=codes) | Q(language__alpha3__in=codes))

    @classmethod
    def filter_extraction_status(cls, qs, name, value):
        from apps.extraction.admin_views import ENTRY_EXTRACTION_STATUSES, annotate_entries
        from apps.extraction.models import ExtractionRun

        values = set(cls._split_csv(value))
        statuses = values & set(ENTRY_EXTRACTION_STATUSES)
        available = "available" in values
        if not statuses and not available:
            return qs
        condition = Q(extraction_status__in=statuses) if statuses else Q(pk__in=[])
        if available:
            condition |= Q(pk__in=ExtractionRun.objects.filter(is_active=True).values("acquisition__entry_id"))
        return annotate_entries(qs).filter(condition)

    # IP-004 Phase 5: saturation post-filters. These are instance methods so they
    # have access to `self.request.user`, which `lcp_state_mapping` needs to
    # compute `active_loan_for_user` correctly.

    def filter_lcp_state(self, qs, name, value):
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        states = {token for token in self._split_csv(value)}
        if not states:
            return qs

        user = self.request.user if self.request is not None else None
        # `lcp_state_mapping` reads only the primary key and `config`.
        materialized = list(qs.only("pk", "config"))
        mapping = lcp_state_mapping(user, materialized)
        matching_ids = [
            entry.pk
            for entry in materialized
            if (row := mapping.get(entry.pk)) is not None
            and (
                str(row["lcp_state"].value if hasattr(row["lcp_state"], "value") else row["lcp_state"]) in states
                or ("reserved" in states and row.get("user_reservation_id") is not None)
            )
        ]
        return qs.filter(pk__in=matching_ids)

    def filter_over_saturated(self, qs, name, value):
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        if value is None:
            return qs

        user = self.request.user if self.request is not None else None
        materialized = list(qs.only("pk", "config"))
        mapping = lcp_state_mapping(user, materialized)
        matching_ids = [
            entry.pk
            for entry in materialized
            if (row := mapping.get(entry.pk)) is not None and bool(row.get("over_saturated")) is bool(value)
        ]
        return qs.filter(pk__in=matching_ids)

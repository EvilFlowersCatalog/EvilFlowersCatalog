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


class EntryFilter(django_filters.FilterSet):
    class Meta:
        model = Entry
        fields = []

    # OpenSearch template configuration
    # TODO: Implement proper OpenSearch template mapping for better API compatibility

    creator_id = django_filters.UUIDFilter()
    catalog_id = django_filters.UUIDFilter()
    catalog_title = django_filters.CharFilter(field_name="catalog__title", lookup_expr="unaccent__icontains")
    author_id = django_filters.UUIDFilter(method="filter_author_id", label=_("Author"))
    author = django_filters.CharFilter(method="filter_author")
    category_id = django_filters.UUIDFilter(label=_("Category"), field_name="categories__id")
    category_term = django_filters.CharFilter(field_name="categories__term")
    language_id = django_filters.UUIDFilter()
    language_code = django_filters.CharFilter(field_name="language__code", label=_("Language"))
    title = django_filters.CharFilter(lookup_expr="unaccent__icontains")
    summary = django_filters.CharFilter(lookup_expr="unaccent__icontains")
    query = django_filters.CharFilter(method="filter_query")
    feed_id = django_filters.UUIDFilter(field_name="feeds__id")
    published_at__gte = django_filters.CharFilter(method="filter_published_at_gte")
    published_at__lte = django_filters.CharFilter(method="filter_published_at_lte")
    config__readium_enabled = django_filters.BooleanFilter(field_name="config__readium_enabled")

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
            url_params["language_code"] = language.code
            facets.append(
                Facet(
                    title=language.name,
                    href=f"{self.request.path}?{urlencode(url_params)}",
                    group=_("Language"),
                    count=self.qs.filter(language=language).count(),
                    is_active=self.request.GET.get("language_code") == language.code,
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

        if not self.request.user.is_authenticated:
            return qs.filter(catalog__is_public=True)

        if not self.request.user.is_superuser:
            qs = qs.filter(Q(catalog__users=self.request.user) | Q(catalog__is_public=True))

        return qs

    @staticmethod
    def filter_author(qs, name, value):
        return (
            qs.annotate(
                author_full_name=Concat(
                    "authors__name", Value(" "), "authors__surname", output_field=CharField()
                )
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
        # Enhanced search with author names and relevance ranking
        search_terms = value.strip().split()
        
        # Build base query with all searchable fields
        base_query = Q()
        
        for term in search_terms:
            term_query = (
                Q(title__unaccent__icontains=term)
                | Q(summary__unaccent__icontains=term)
                | Q(content__unaccent__icontains=term)
                | Q(publisher__unaccent__icontains=term)
                | Q(feeds__title__unaccent__icontains=term)
                | Q(categories__label__unaccent__icontains=term)
                | Q(categories__term__unaccent__icontains=term)
            )
            base_query &= term_query
        
        # Add author search with concatenated full name
        author_query = Q()
        for term in search_terms:
            author_query |= (
                Q(authors__name__unaccent__icontains=term)
                | Q(authors__surname__unaccent__icontains=term)
            )
        
        # Combine with full name search
        full_query = base_query | author_query
        
        # Apply filtering and add relevance ranking
        return (
            qs.annotate(
                author_full_name=Concat(
                    "authors__name", Value(" "), "authors__surname", output_field=CharField()
                )
            )
            .filter(
                full_query
                | Q(author_full_name__unaccent__icontains=value)
            )
            .annotate(
                # Relevance scoring: title matches get highest priority
                relevance_score=Case(
                    When(title__unaccent__icontains=value, then=Value(100)),
                    When(author_full_name__unaccent__icontains=value, then=Value(90)),
                    When(authors__name__unaccent__icontains=value, then=Value(80)),
                    When(authors__surname__unaccent__icontains=value, then=Value(80)),
                    When(publisher__unaccent__icontains=value, then=Value(70)),
                    When(categories__label__unaccent__icontains=value, then=Value(60)),
                    When(summary__unaccent__icontains=value, then=Value(50)),
                    When(content__unaccent__icontains=value, then=Value(40)),
                    When(feeds__title__unaccent__icontains=value, then=Value(30)),
                    default=Value(10),
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

from typing import List
from urllib.parse import urlencode

import django_filters
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _
from partial_date import PartialDate

from apps.core.models import Entry, Language, Category, Author
from apps.opds.structures import Facet


class EntryFilter(django_filters.FilterSet):
    class Meta:
        model = Entry
        fields = []

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
    published_at_gte = django_filters.CharFilter(method="filter_published_at_gte")
    published_at_lte = django_filters.CharFilter(method="filter_published_at_lte")

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
        return qs.filter(Q(authors__name__unaccent__icontains=value) | Q(authors__surname__unaccent__icontains=value))

    @staticmethod
    def filter_author_id(qs, name, value):
        return qs.filter(authors__id=value)

    @staticmethod
    def filter_query(qs, name, value):
        return qs.filter(
            Q(title__unaccent__icontains=value)
            | Q(summary__unaccent__icontains=value)
            | Q(feeds__title__icontains=value)
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

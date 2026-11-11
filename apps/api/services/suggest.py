"""Type-ahead suggestions (`GET /api/v1/entries/suggest`, IP-020): one call instead of five."""

from typing import Optional
from uuid import UUID

from django.db.models import Count, F, Q, Value
from django.db.models.functions import Concat
from django.http import QueryDict

from apps.api.filters.entries import EntryFilter
from apps.core.fields.localized import LocalizedValues
from apps.core.models import Author, Category, Entry, Feed
from apps.core.search.query import SearchState, describe_hits, did_you_mean, words


class EntrySuggestService:
    def __init__(self, request, text: str, *, limit: int = 6, catalog_id: Optional[str] = None):
        self.request = request
        self.text = text
        self.limit = limit
        self.catalog_id = self._uuid(catalog_id)

    @staticmethod
    def _uuid(value) -> Optional[UUID]:
        try:
            return UUID(str(value)) if value else None
        except ValueError:
            return None

    def compute(self) -> dict:
        result = {
            "query": self.text,
            "entries": [],
            "authors": [],
            "categories": [],
            "feeds": [],
            "did_you_mean": None,
        }
        if not words(self.text):
            return result

        base = Entry.objects.all()
        if self.catalog_id is not None:
            base = base.filter(catalog_id=self.catalog_id)
        entry_filter = EntryFilter(QueryDict(), queryset=base, request=self.request)
        readable = entry_filter.qs
        catalogs = entry_filter.get_accessible_catalog_ids()  # None: every catalog (superuser)

        result["entries"] = self._entries(readable)
        result["authors"] = self._authors(catalogs)
        result["categories"] = self._categories(catalogs)
        result["feeds"] = self._feeds(catalogs)
        if not result["entries"]:
            result["did_you_mean"] = did_you_mean(self.text, readable)
        return result

    def _scope(self, qs, catalogs):
        if catalogs is not None:
            qs = qs.filter(catalog_id__in=catalogs)
        if self.catalog_id is not None:
            qs = qs.filter(catalog_id=self.catalog_id)
        return qs

    def _entries(self, readable) -> list[dict]:
        # Type-ahead is a prefix task: it never waits for TEI (#134), a cached query vector still helps.
        search = SearchState(self.text, prefix_last=True, cached_vector_only=True)
        entries = list(
            search.apply(readable)
            .order_by("-relevance_score", "-popularity", "-pk")
            .prefetch_related("entry_authors__author")[: self.limit]
        )
        hits = describe_hits(search, entries)
        return [
            {
                "id": entry.pk,
                "catalog_id": entry.catalog_id,
                "title": entry.title,
                "authors": [
                    ea.author.full_name for ea in sorted(entry.entry_authors.all(), key=lambda ea: ea.position)
                ],
                "thumbnail": self.request.build_absolute_uri(entry.thumbnail_url) if entry.thumbnail_url else None,
                "highlight": (hits.get(entry.pk) or {}).get("highlights", {}).get("title"),
            }
            for entry in entries
        ]

    def _authors(self, catalogs) -> list[dict]:
        rows = (
            self._scope(Author.objects.all(), catalogs)
            .annotate(full_name=Concat(F("name"), Value(" "), F("surname")))
            .filter(full_name__unaccent__icontains=self.text)
            .annotate(count=Count("entries", distinct=True))
            .filter(count__gt=0)
            .order_by("-count", "surname", "name")
            .values("id", "name", "surname", "count")[: self.limit]
        )
        return list(rows)

    def _categories(self, catalogs) -> list[dict]:
        rows = (
            self._scope(Category.objects.all(), catalogs)
            .annotate(label_text=LocalizedValues(F("label")))
            .filter(Q(term__unaccent__icontains=self.text) | Q(label_text__unaccent__icontains=self.text))
            .annotate(count=Count("entries", distinct=True))
            .filter(count__gt=0)
            .order_by("-count", "term")
            .values("id", "term", "label", "count")[: self.limit]
        )
        return list(rows)

    def _feeds(self, catalogs) -> list[dict]:
        rows = (
            self._scope(Feed.objects.all(), catalogs)
            .annotate(title_text=LocalizedValues(F("title")))
            .filter(title_text__unaccent__icontains=self.text)
            .annotate(count=Count("entries", distinct=True))
            .filter(count__gt=0)
            .order_by("-count", "url_name")
            .values("id", "title", "count")[: self.limit]
        )
        return list(rows)

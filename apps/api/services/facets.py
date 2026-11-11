"""
Facet counts for entry filters, shared by `GET /api/v1/entries/facets`, `GET /api/v1/entries?include=facets`
and the OPDS 1.2 / OPDS 2.0 feeds (IP-020), so every surface reports the same numbers.

Each dimension is counted with every active filter applied except its own (disjunctive faceting):
selecting a language still shows the other languages that could be OR-ed in.
"""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional
from uuid import UUID

from django.core.exceptions import EmptyResultSet
from django.db import connection
from django.db.models import Count, Exists, F, Func, IntegerField, Max, Min, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Concat
from django.http import QueryDict
from django.utils.translation import gettext_lazy as _

from apps.api.filters.entries import EntryFilter, fulltext_runs
from apps.api.serializers.entries import LcpState
from apps.core.fields.localized import localize
from apps.core.models import Entry
from apps.core.search.query import SearchState

DEFAULT_FACET_LIMIT = 10
MAX_FACET_LIMIT = 100


@dataclass(frozen=True)
class Dimension:
    #: EntryFilter parameter that filters on this dimension (ignored while counting it)
    param: str
    #: grouped field
    field: str
    #: further fields read per value (labels, names)
    details: tuple = ()
    #: lookups `facet_query` searches in
    search: tuple = ()
    #: limited to `facet_limit` values even when the client sends none (open-ended dimensions)
    limited: bool = False
    #: OPDS facet group title
    title: str = ""


DIMENSIONS = {
    "languages": Dimension(
        "language_code",
        "language__alpha2",
        ("language__name",),
        ("language__name__unaccent__icontains",),
        title=_("Language"),
    ),
    "categories": Dimension(
        "category_id",
        "categories__id",
        ("categories__term", "categories__label"),
        ("categories__term__unaccent__icontains", "categories__label__icontains"),
        title=_("Category"),
    ),
    "feeds": Dimension("feed_id", "feeds__id", ("feeds__title",), ("feeds__title__icontains",), title=_("Feed")),
    "authors": Dimension(
        "author_id",
        "authors__id",
        ("authors__name", "authors__surname"),
        ("author_full_name__unaccent__icontains",),
        limited=True,
        title=_("Author"),
    ),
    "publishers": Dimension(
        "publisher", "publisher", (), ("publisher__unaccent__icontains",), limited=True, title=_("Publisher")
    ),
    "formats": Dimension("format", "acquisitions__mime", (), ("acquisitions__mime__icontains",), title=_("Format")),
}

SPECIAL = ("availability", "fulltext", "years")
ALL_FACETS = (*DIMENSIONS, *SPECIAL)


class EntryFacetService:
    def __init__(
        self,
        request,
        params: Optional[QueryDict] = None,
        *,
        queryset: Optional[QuerySet] = None,
        search: Optional[SearchState] = None,
    ):
        """
        `params`: the entry filter parameters (default `request.GET`), plus the facet options
        `facet` (comma-separated dimensions to compute), `facet_limit` and `facet_query`.
        `queryset`: the entries to facet (default all; OPDS passes one catalog).
        `search`: the resolved free-text search of the main result, so a relaxed or fuzzy result
        is faceted with the same level instead of being re-resolved per dimension.
        """
        self.request = request
        self.params = (params if params is not None else request.GET).copy()
        self.queryset = queryset if queryset is not None else Entry.objects.all()
        self.search = search

        requested = [name.strip() for name in self.params.get("facet", "").split(",") if name.strip()]
        self.dimensions = [name for name in ALL_FACETS if name in requested] if requested else list(ALL_FACETS)
        self.facet_query = (self.params.get("facet_query") or "").strip()
        self.explicit_limit = self._parse_limit(self.params.get("facet_limit"))

        for option in ("facet", "facet_limit", "facet_query", "include", "page", "limit", "order_by"):
            self.params.pop(option, None)

        #: `entries_without` per effective filter parameters: a dimension whose own filter is not in the
        #: request counts the same entries as every other such dimension, so they share one EntryFilter.
        self._matching: dict[tuple, QuerySet] = {}

    @staticmethod
    def _parse_limit(raw) -> Optional[int]:
        try:
            return max(1, min(int(raw), MAX_FACET_LIMIT)) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _limit(self, dimension: Dimension) -> Optional[int]:
        if self.explicit_limit is not None:
            return self.explicit_limit
        return DEFAULT_FACET_LIMIT if dimension.limited else None

    def entries_without(self, *params) -> QuerySet:
        """
        Entries matching every active filter except `params` (a facet ignores its own filter).

        Building an EntryFilter (filter copies, form, validation, query) is most of the facets' CPU, and
        without the dimension's own filter in the request the parameters are those of the whole request:
        the filtered queryset is built once per distinct parameter set and reused as the subquery.
        """
        query = self.params.copy()
        for param in params:
            query.pop(param, None)
        key = tuple(sorted((name, tuple(values)) for name, values in query.lists()))
        matching = self._matching.get(key)
        if matching is None:
            matching = self._matching[key] = (
                EntryFilter(query, queryset=self.queryset, request=self.request, search=self.search)
                .qs.order_by()
                .values("pk")
            )
        # `pk__in` clones the subquery into each outer query, so sharing `matching` is safe.
        return Entry.objects.filter(pk__in=matching)

    def _resolve_search(self) -> None:
        """
        Without the main result's search (`GET /entries/facets?query=`), resolve it once here: otherwise
        every dimension would resolve the fallback levels and rank the query on its own (#135).
        """
        if self.search is None and (self.params.get("query") or "").strip():
            main = EntryFilter(self.params, queryset=self.queryset, request=self.request)
            main.qs  # noqa: B018 -- resolves the level and records what each channel found
            self.search = main.search

    def branches(self) -> dict[str, QuerySet]:
        """The counting query of every requested dimension computed in SQL (all but availability)."""
        branches = {name: self._grouped_rows(DIMENSIONS[name]) for name in self.dimensions if name in DIMENSIONS}
        if "fulltext" in self.dimensions:
            branches["fulltext"] = self._fulltext_rows()
        if "years" in self.dimensions:
            branches["years"] = self._years_rows()
        return branches

    def compute(self) -> dict:
        self._resolve_search()
        rows = count_together(self.branches())
        result = {}
        for name in self.dimensions:
            if name in DIMENSIONS:
                result[name] = [self._grouped_item(DIMENSIONS[name], row) for row in rows[name]]
        if "availability" in self.dimensions:
            result["availability"] = self._availability()
        if "fulltext" in self.dimensions:
            result["fulltext"] = self._fulltext(rows["fulltext"])
        if "years" in self.dimensions:
            result["years"] = self._years(rows["years"])
        return result

    def _grouped_rows(self, dimension: Dimension) -> QuerySet:
        qs = self.entries_without(dimension.param).filter(**{f"{dimension.field}__isnull": False})
        if dimension.field == "publisher":
            qs = qs.exclude(publisher="")
        if self.facet_query and dimension.search:
            if dimension.param == "author_id":
                qs = qs.annotate(author_full_name=Concat(F("authors__name"), Value(" "), F("authors__surname")))
            condition = Q()
            for lookup in dimension.search:
                condition |= Q(**{lookup: self.facet_query})
            qs = qs.filter(condition)

        rows = (
            qs.values(dimension.field, *dimension.details)
            .annotate(count=Count("pk", distinct=True))
            .order_by("-count", dimension.field)
        )
        limit = self._limit(dimension)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def _grouped_item(self, dimension: Dimension, row: dict) -> dict:
        value = row[dimension.field]
        if dimension.field.endswith("__id") and value is not None:
            value = UUID(str(value))  # JSON carries the UUID as a string
        return {"value": value, "count": row["count"], **self._details(dimension, row)}

    @staticmethod
    def _details(dimension: Dimension, row: dict) -> dict:
        if dimension.param == "language_code":
            return {"name": row["language__name"]}
        if dimension.param == "category_id":
            return {"term": row["categories__term"], "label": row["categories__label"] or {}}
        if dimension.param == "feed_id":
            return {"title": row["feeds__title"] or {}}
        if dimension.param == "author_id":
            return {"name": row["authors__name"], "surname": row["authors__surname"]}
        return {}

    def _availability(self) -> list[dict]:
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        # Only LCP titles have an availability state; `lcp_state_mapping` reads just pk + config.
        entries = self.entries_without("lcp_state").filter(config__readium_enabled=True).only("pk", "config")
        states = Counter()
        for row in lcp_state_mapping(self.request.user, entries).values():
            state = row["lcp_state"]
            states[state.value if hasattr(state, "value") else str(state)] += 1
            if row.get("user_reservation_id") is not None:
                states["reserved"] += 1
        states.pop(LcpState.NOT_LCP.value, None)
        return [{"state": state, "count": count} for state, count in states.most_common() if count]

    def _fulltext_rows(self) -> QuerySet:
        # A constant in `values()` and no grouped field: one aggregate row (Django leaves out GROUP BY).
        return (
            self.entries_without("fulltext")
            .values(_all=Value(1, output_field=IntegerField()))
            .annotate(
                total=Count("pk"),
                extracted=Count("pk", filter=Q(Exists(fulltext_runs().filter(acquisition__entry_id=OuterRef("pk"))))),
            )
        )

    @staticmethod
    def _fulltext(rows: list[dict]) -> list[dict]:
        counts = rows[0] if rows else {"total": 0, "extracted": 0}
        result = [
            {"value": True, "count": counts["extracted"]},
            {"value": False, "count": counts["total"] - counts["extracted"]},
        ]
        return [row for row in result if row["count"]]

    def _years_rows(self) -> QuerySet:
        # `published_at` is a PartialDateField (a plain Field, so ExtractYear rejects it); the column is a
        # timestamptz, read in the connection's time zone (UTC) as the field's converter does.
        year = Func(F("published_at"), template="EXTRACT(YEAR FROM %(expressions)s)::int", output_field=IntegerField())
        return (
            self.entries_without("published_at__gte", "published_at__lte")
            .values(_all=Value(1, output_field=IntegerField()))
            .annotate(min=Min(year), max=Max(year))
        )

    @staticmethod
    def _years(rows: list[dict]) -> dict:
        bounds = rows[0] if rows else {}
        return {"min": bounds.get("min"), "max": bounds.get("max")}


def count_together(branches: dict[str, QuerySet]) -> dict[str, list[dict]]:
    """
    Run the facet queries as one statement (`UNION ALL`, one row per facet value as JSON) instead of one
    round trip per dimension (#135). Rows keep each branch's own order; keys are the `values()` names.
    """
    names = list(branches)
    result: dict[str, list[dict]] = {name: [] for name in names}
    parts, params = [], []
    for index, qs in enumerate(branches.values()):
        try:
            sql, branch_params = qs.query.sql_with_params()
        except EmptyResultSet:  # nothing to count (e.g. a search without hits)
            continue
        parts.append(
            f"(SELECT {index} AS branch, row_number() OVER () AS position, to_jsonb(t) AS row FROM ({sql}) AS t)"
        )
        params.extend(branch_params)
    if not parts:
        return result
    with connection.cursor() as cursor:
        cursor.execute(f"{' UNION ALL '.join(parts)} ORDER BY branch, position", params)
        for branch, _position, row in cursor.fetchall():
            result[names[branch]].append(json.loads(row) if isinstance(row, str) else row)
    return result


@dataclass
class FacetLink:
    """One facet value, ready for an OPDS link: the parameter to set, its value, a title and a count."""

    group: str
    param: str
    value: str
    title: str
    count: int
    active: bool


OPDS_DIMENSIONS = ("languages", "categories", "feeds", "authors", "formats")


def opds_facets(request, facets: dict, language: Optional[str] = None) -> list[FacetLink]:
    """Facet values as links; a value is active when the request already filters on exactly it."""
    titles: dict[str, Callable[[dict], str]] = {
        "languages": lambda row: row.get("name") or row["value"],
        "categories": lambda row: localize(row.get("label"), language) or row.get("term") or str(row["value"]),
        "feeds": lambda row: localize(row.get("title"), language) or str(row["value"]),
        "authors": lambda row: f"{row.get('name', '')} {row.get('surname', '')}".strip(),
        "formats": lambda row: str(row["value"]),
    }
    links = []
    for name in OPDS_DIMENSIONS:
        dimension = DIMENSIONS[name]
        current = request.GET.get(dimension.param)
        for row in facets.get(name, []):
            value = str(row["value"])
            links.append(
                FacetLink(
                    group=str(dimension.title),
                    param=dimension.param,
                    value=value,
                    title=titles[name](row),
                    count=row["count"],
                    active=current == value,
                )
            )
    return links


def facet_href(request, link: FacetLink, base: Optional[str] = None) -> str:
    """The current URL (first page) with the facet applied; for the active facet that is the current URL."""
    params = request.GET.copy()
    params.pop("page", None)
    params[link.param] = link.value
    base = base if base is not None else request.path
    query = params.urlencode()
    return f"{base}?{query}" if query else base


__all__ = ["ALL_FACETS", "DIMENSIONS", "EntryFacetService", "FacetLink", "facet_href", "opds_facets"]

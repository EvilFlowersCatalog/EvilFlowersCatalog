"""Argument autocompletion (`completion/complete`).

The single most common way a model wastes a turn against this catalog is
inventing a value: a category term that does not exist, a language code the
corpus does not use, a catalog id it half-remembers. Completion closes that
loop against live data — the client asks "what could `category_term` be, given
the user has typed 'mat'?" and gets real terms back.

Only arguments with a bounded, knowable domain are completable. A free-text
`query` has no completions and correctly returns an empty list.

Completion runs under the caller's own access control: suggestions are drawn
through the same filters as the tools, so it cannot leak the existence of a
category in a catalog the caller may not read.
"""

from typing import Callable, Dict, List, Optional

from django.db.models import Q
from django.http import QueryDict

from apps.api.filters.authors import AuthorFilter
from apps.api.filters.catalogs import CatalogFilter
from apps.api.filters.categories import CategoryFilter
from apps.core.models import Author, Catalog, Category, Language
from apps.mcp.projections import author_name

#: The protocol caps a completion response at 100 values.
MAX_VALUES = 100


def _catalog_ids(request, partial: str) -> List[str]:
    params = QueryDict(mutable=True)
    if partial:
        params["title"] = partial
    return [
        str(pk)
        for pk in CatalogFilter(params, queryset=Catalog.objects.all(), request=request)
        .qs.order_by("title")
        .values_list("id", flat=True)[:MAX_VALUES]
    ]


def _catalog_titles(request, partial: str) -> List[str]:
    params = QueryDict(mutable=True)
    if partial:
        params["title"] = partial
    return list(
        CatalogFilter(params, queryset=Catalog.objects.all(), request=request)
        .qs.order_by("title")
        .values_list("title", flat=True)[:MAX_VALUES]
    )


def _category_terms(request, partial: str) -> List[str]:
    params = QueryDict(mutable=True)
    if partial:
        params["query"] = partial
    return list(
        CategoryFilter(params, queryset=Category.objects.all(), request=request)
        .qs.order_by("term")
        .values_list("term", flat=True)
        .distinct()[:MAX_VALUES]
    )


def _category_ids(request, partial: str) -> List[str]:
    params = QueryDict(mutable=True)
    if partial:
        params["query"] = partial
    return [
        str(pk)
        for pk in CategoryFilter(params, queryset=Category.objects.all(), request=request)
        .qs.order_by("term")
        .values_list("id", flat=True)[:MAX_VALUES]
    ]


def _author_names(request, partial: str) -> List[str]:
    params = QueryDict(mutable=True)
    if partial:
        params["query"] = partial
    authors = (
        AuthorFilter(params, queryset=Author.objects.all(), request=request)
        .qs.order_by("surname", "name")
        .distinct()[:MAX_VALUES]
    )
    return [author_name(item) for item in authors]


def _language_codes(request, partial: str) -> List[str]:
    """Languages actually present in the catalog, not the whole ISO register.

    Completing to a code no publication uses would be worse than no completion
    at all — it looks like a valid filter and returns nothing.
    """
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry

    readable = EntryFilter(QueryDict(mutable=True), queryset=Entry.objects.all(), request=request).qs
    languages = Language.objects.filter(entries__in=readable).distinct()
    if partial:
        languages = languages.filter(
            Q(alpha2__istartswith=partial) | Q(alpha3__istartswith=partial) | Q(name__icontains=partial)
        )
    return list(languages.order_by("alpha2").values_list("alpha2", flat=True)[:MAX_VALUES])


#: (reference kind, reference name, argument name) -> resolver.
#: Keyed loosely by argument name because the same argument means the same
#: thing across tools — `catalog_id` is a catalog id everywhere.
_RESOLVERS: Dict[str, Callable] = {
    "catalog_id": _catalog_ids,
    "catalog_title": _catalog_titles,
    "catalog": _catalog_titles,
    "category_term": _category_terms,
    "category_ids": _category_ids,
    "term": _category_terms,
    "author": _author_names,
    "language_codes": _language_codes,
}


def complete(request, reference: Optional[dict], argument: Optional[dict]) -> dict:
    """Answer `completion/complete`.

    An unknown argument is not an error — the protocol expects an empty list,
    and a client asking about every argument it renders is normal behaviour.
    """
    argument = argument or {}
    name = argument.get("name")
    partial = argument.get("value") or ""

    resolver = _RESOLVERS.get(name)
    values = resolver(request, partial) if resolver else []

    return {
        "completion": {
            "values": values[:MAX_VALUES],
            "total": len(values),
            "hasMore": len(values) >= MAX_VALUES,
        }
    }


__all__ = ["complete"]

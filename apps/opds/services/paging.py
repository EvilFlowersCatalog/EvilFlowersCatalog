"""
RFC 5005 §3 paging for OPDS 1.2 acquisition feeds (OPDS 1.2 §2.4).

A page is resolved in two steps so the conditional-GET check can run
between them:

1. `FeedPage.resolve` runs a COUNT and one `values_list("pk", "touched_at")`
   slice. Those rows feed the ETag (`apps/opds/services/conditional.py`).
2. `FeedPage.load` fetches the page's entries with the acquisition prefetch
   and restores the order of step 1. It only runs when the client's cached
   copy is stale.
"""

from dataclasses import dataclass
from http import HTTPStatus
from typing import List, Optional, Tuple
from urllib.parse import urlencode

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import QuerySet
from django.utils.translation import gettext as _

from apps.core.errors import ProblemDetailException
from apps.opds.schema import Link, LinkType

PAGE_PARAM = "page"
ACQUISITION_FEED_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"


@dataclass(frozen=True)
class FeedPage:
    # `(pk, touched_at)` per entry on the page, in feed order
    rows: List[Tuple]
    number: int
    num_pages: int

    @property
    def is_paged(self) -> bool:
        return self.num_pages > 1

    @property
    def entry_ids(self) -> list:
        return [pk for pk, _ in self.rows]

    @classmethod
    def resolve(cls, request, qs: QuerySet, per_page: int) -> "FeedPage":
        """Resolve `?page=` against an ordered queryset of entries.

        The queryset must be totally ordered (end the ordering on `pk`) or
        rows can repeat or vanish between pages.
        """
        paginator = Paginator(qs.values_list("pk", "touched_at"), per_page)
        try:
            page = paginator.page(request.GET.get(PAGE_PARAM, 1))
        except PageNotAnInteger:
            raise ProblemDetailException(_("Invalid page number"), status=HTTPStatus.BAD_REQUEST)
        except EmptyPage:
            raise ProblemDetailException(_("Page not found"), status=HTTPStatus.NOT_FOUND)
        return cls(rows=list(page.object_list), number=page.number, num_pages=paginator.num_pages)

    @classmethod
    def whole(cls, qs: QuerySet, limit: Optional[int] = None) -> "FeedPage":
        """The first `limit` entries of `qs` (all when `None`) as one unpaged page."""
        rows = qs.values_list("pk", "touched_at")
        if limit is not None:
            rows = rows[:limit]
        return cls(rows=list(rows), number=1, num_pages=1)

    def load(self, qs: QuerySet) -> list:
        """The page's models from `qs` (already prefetched), in page order."""
        position = {pk: index for index, pk in enumerate(self.entry_ids)}
        rows = qs.filter(pk__in=self.entry_ids)
        return sorted(rows, key=lambda row: position[row.pk])

    def links(self, request, path: str, link_type: str = ACQUISITION_FEED_TYPE) -> List[Link]:
        """`first`/`previous`/`next`/`last` links; none for an unpaged feed.

        Other query parameters (an `access_token`, say) are carried over so
        a client that authenticates by query string can follow the links.
        """
        if not self.is_paged:
            return []

        def href(number: int) -> str:
            params = request.GET.copy()
            params[PAGE_PARAM] = str(number)
            return f"{path}?{urlencode(sorted(params.items()))}"

        links = [Link(rel=LinkType.FIRST, href=href(1), type=link_type)]
        if self.number > 1:
            links.append(Link(rel=LinkType.PREVIOUS, href=href(self.number - 1), type=link_type))
        if self.number < self.num_pages:
            links.append(Link(rel=LinkType.NEXT, href=href(self.number + 1), type=link_type))
        links.append(Link(rel=LinkType.LAST, href=href(self.num_pages), type=link_type))
        return links

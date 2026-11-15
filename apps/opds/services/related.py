"""Similar publications as `related` links, so every OPDS reader shows them (#160).

The neighbours are the library map's edges (`apps.graph.views.entry_neighbours`, mutual kNN of centred
per-language publication vectors, #151/#152), computed over what the caller may see *within the entry's own
catalog*: a publication of another (e.g. private) catalog is never a neighbour, and its presence never changes a
visible pair. Only entries with an active extraction run take part; others get no related links.

One `RelatedEntries` per response: the neighbours of a catalog are worked out once, however many entries of it
the response renders. The map itself is cached by its ETag (the active runs), so a repeat is cheap.
"""

import logging
from typing import Dict, Iterable, List, Tuple

from apps.core.models import Entry

logger = logging.getLogger(__name__)

#: Related links per publication.
MAX_RELATED = 4


class RelatedEntries:
    def __init__(self, request):
        self.request = request
        self._catalogs: Dict[str, Tuple[Dict[str, List[tuple]], Dict[str, str]]] = {}

    def _catalog(self, catalog_id) -> Tuple[Dict[str, List[tuple]], Dict[str, str]]:
        key = str(catalog_id)
        if key not in self._catalogs:
            from apps.graph.views import entry_neighbours

            try:
                neighbours = entry_neighbours(self.request, {"catalog_id": key})
            except Exception:
                # Related links are a courtesy: a broken similarity layer must not take the feed down.
                logger.exception("Related publications of catalog %s could not be computed", key)
                neighbours = {}
            ids = {other for items in neighbours.values() for other, _score in items[:MAX_RELATED]}
            titles = {str(pk): title for pk, title in Entry.objects.filter(pk__in=ids).values_list("pk", "title")}
            self._catalogs[key] = (neighbours, titles)
        return self._catalogs[key]

    @classmethod
    def for_request(cls, request) -> "RelatedEntries":
        """The request's own instance, so the feed's ETag and its rendering work the neighbours out once."""
        if request is None:
            return cls(None)
        found = getattr(request, "_opds_related", None)
        if found is None:
            found = cls(request)
            request._opds_related = found
        return found

    def _of(self, catalog_id, entry_id) -> List[Tuple[str, str]]:
        if self.request is None:
            return []
        neighbours, titles = self._catalog(catalog_id)
        return [
            (other, titles[other])
            for other, _score in neighbours.get(str(entry_id), [])[:MAX_RELATED]
            if other in titles
        ]

    def of(self, entry: Entry) -> List[Tuple[str, str]]:
        """`[(entry_id, title)]` of up to `MAX_RELATED` publications like `entry`, best first."""
        return self._of(entry.catalog_id, entry.pk)

    def signature(self, entry_ids: Iterable) -> List[str]:
        """What the related links of these entries render as, for a feed's ETag (`conditional.feed_etag`)."""
        if self.request is None:
            return []
        rows = Entry.objects.filter(pk__in=list(entry_ids)).values_list("pk", "catalog_id")
        return [
            f"related:{entry_id}>{other}:{title}"
            for entry_id, catalog_id in sorted(rows, key=lambda row: str(row[0]))
            for other, title in self._of(catalog_id, entry_id)
        ]

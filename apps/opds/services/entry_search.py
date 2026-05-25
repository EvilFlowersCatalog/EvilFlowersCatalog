from django.db.models import QuerySet

from apps.api.filters.entries import EntryFilter
from apps.core.models import Catalog, Entry


class EntrySearchService:
    """Shared catalog-DB search for OPDS 1.2 and OPDS 2.0.

    Wraps `EntryFilter` so OPDS 1.2 (Atom) and OPDS 2.0 (RWPM) consume the same
    query layer. Response shape diverges per profile; only the query layer is shared.
    """

    @staticmethod
    def search(catalog: Catalog, request) -> QuerySet[Entry]:
        queryset = Entry.objects.filter(catalog=catalog)
        return EntryFilter(request.GET, queryset=queryset, request=request).qs

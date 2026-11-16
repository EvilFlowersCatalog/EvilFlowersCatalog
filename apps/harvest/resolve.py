"""The generic resolver (IP-034): `GET /resolve/<source slug>?id=<record id>` → 302 to the portal entry linked to
that record, so a remote system can link "open in Elvira" knowing only its own record id. `?pid=` is accepted as
an alias of `?id=` (external-tool configurations that send a persistent id). Anonymous; withdrawn entries and
unknown ids answer 404."""

from django.conf import settings
from django.http import Http404, HttpResponseRedirect
from django.views import View

from apps.core import settings_store
from apps.core.models import Entry
from apps.harvest.models import HarvestSource


def portal_entry_url(entry_id) -> str:
    """`content.cite_base_url`'s entry detail, or `EVILFLOWERS_PORTAL_URL`'s when that is empty."""
    from apps.content.citation import entry_url

    if (settings_store.get("content.cite_base_url") or "").strip():
        return entry_url(entry_id)
    base = (getattr(settings, "EVILFLOWERS_PORTAL_URL", "") or "").rstrip("/")
    return f"{base}/library?entry-detail-id={entry_id}"


class ResolveView(View):
    def get(self, request, source_slug: str):
        record_id = (request.GET.get("id") or request.GET.get("pid") or "").strip()
        source = HarvestSource.objects.filter(slug=source_slug).first()
        if source is None or not record_id:
            raise Http404("Unknown source or record")
        entry_id = (
            Entry.objects.listed()
            .filter(catalog_id=source.target_catalog_id, **{f"identifiers__{source.system_key}": record_id})
            .values_list("pk", flat=True)
            .first()
        )
        if entry_id is None:
            raise Http404("No entry for this record")
        return HttpResponseRedirect(portal_entry_url(entry_id))


__all__ = ["ResolveView", "portal_entry_url"]

"""`/.well-known/tdmrep.json` (#177): the W3C TDMRep Community Group report's site-wide file.

One rule per publication of a public catalog whose rights holder reserved text and data mining
(`entry.rights.tdm_reservation = 1`), covering the paths a crawler can reach of it: its OPDS 1.2 entry,
its OPDS 2 publication and manifest, and its file downloads. Each rule carries `tdm-reservation: 1` and, where
the rights holder published one, `tdm-policy`. Nothing is asserted for other paths: a missing reservation is
not the same as a known absence of one.
"""

import json

from django.core.cache import cache
from django.http import HttpResponse
from django.urls import reverse
from django.views import View

from apps.core import settings_store

CACHE_KEY = "content-tdmrep:v1"
CACHE_TIMEOUT = 60 * 60


def rules() -> list:
    from apps.core.models import Entry

    entries = (
        Entry.objects.filter(catalog__is_public=True, rights__tdm_reservation=1)
        .select_related("catalog")
        .prefetch_related("acquisitions")
        .order_by("pk")
    )
    result = []
    for entry in entries:
        catalog = entry.catalog.url_name
        locations = [
            reverse("opds:complete-entry", kwargs={"catalog_name": catalog, "entry_id": entry.pk}),
            reverse("opds2:publication-detail", kwargs={"catalog_name": catalog, "entry_id": entry.pk}),
            reverse("opds2:publication-manifest", kwargs={"catalog_name": catalog, "entry_id": entry.pk}),
        ] + [
            reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})
            for acquisition in entry.acquisitions.all()
        ]
        policy = (entry.rights or {}).get("tdm_policy_url")
        for location in locations:
            rule = {"location": location, "tdm-reservation": 1}
            if policy:
                rule["tdm-policy"] = policy
            result.append(rule)
    return result


def invalidate() -> None:
    cache.delete(settings_store.database_cache_key(CACHE_KEY))


class TDMRepView(View):
    """Public, unauthenticated: crawlers read it before mining."""

    def get(self, request):
        key = settings_store.database_cache_key(CACHE_KEY)
        body = cache.get(key)
        if body is None:
            body = json.dumps(rules(), ensure_ascii=False, indent=2)
            cache.set(key, body, CACHE_TIMEOUT)
        response = HttpResponse(body, content_type="application/json")
        response["Cache-Control"] = "public, max-age=3600"
        return response


__all__ = ["TDMRepView", "invalidate", "rules"]

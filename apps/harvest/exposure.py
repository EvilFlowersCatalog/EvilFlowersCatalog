"""IP-033 Q7 as a per-source flag: entries a source created (`origin = harvest:<slug>`) are not offered over
Elvira's own OAI-PMH when the source has `oai_reexpose = False` (a repository that is an OAI provider itself)."""

from django.core.cache import cache

CACHE_KEY = "harvest:not_reexposed_origins"
CACHE_SECONDS = 60


def not_reexposed_origins() -> set[str]:
    """`{"harvest:<slug>", …}` of sources with `oai_reexpose = False`; cached for a minute."""
    origins = cache.get(CACHE_KEY)
    if origins is None:
        from apps.harvest.models import HarvestSource

        origins = sorted(
            f"harvest:{slug}"
            for slug in HarvestSource.objects.filter(oai_reexpose=False).values_list("slug", flat=True)
        )
        cache.set(CACHE_KEY, origins, CACHE_SECONDS)
    return set(origins)


def forget() -> None:
    """Drop the cached set (a source's flag changed)."""
    cache.delete(CACHE_KEY)


__all__ = ["forget", "not_reexposed_origins"]

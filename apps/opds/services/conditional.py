"""
Pre-render conditional GET for OPDS 1.2 feeds (issue #72).

An OPDS feed is per user: for each LCP title the requesting user holds a
READY/ACTIVE license on, the feed carries a `.lcpl` link with a freshly
minted capability token (`BorrowLinkResolver.license_url`). So the ETag is
computed from everything that changes the rendered XML for *this* request,
before rendering:

- the feed head (its id and timestamps),
- the page's entries and their `touched_at` (bumped by every `Entry.save()`
  and by the `Acquisition` post_save signal; a removed entry drops out of
  the id list). Known gap: author/category M2M edits and acquisition
  deletes that don't also save the entry leave `touched_at` alone, so a
  client keeps its cached page until something else on it changes,
- the response language (feed titles and category labels are localized),
- the user, and the user's READY/ACTIVE licenses among the page's entries.
  The license *set* is used rather than `License.updated_at`: membership
  is what the feed depends on, and it changes whether a state transition
  went through `save()` or a bulk `QuerySet.update()`.

Tokens expire (`EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS`), so a
user holding licenses also gets a time bucket of half the TTL. Every token in
a copy the client revalidates was minted inside the current bucket, so it
still has at least half its TTL left when the bucket rolls over and the
client gets a fresh render.

No `Last-Modified` is sent: a `touched_at`-based date would 304 a user who
has just borrowed a title, and a client sending only `If-Modified-Since`
would never see the `.lcpl` link.
"""

import hashlib
from typing import Iterable, Optional

from django.utils import timezone
from django.utils.cache import get_conditional_response, patch_cache_control, patch_vary_headers
from django.utils.http import quote_etag

from apps.core.fields.localized import request_language
from apps.readium.capability_scopes import lcpl_feed_download_ttl
from apps.readium.models import License

# Bump when the rendered XML changes shape for the same inputs, so cached
# copies from the previous release are not revalidated as current.
ETAG_VERSION = "opds1-v1"

LICENSED_STATES = (License.LicenseState.READY, License.LicenseState.ACTIVE)


def _token_bucket() -> int:
    half_ttl = max(lcpl_feed_download_ttl() // 2, 1)
    return int(timezone.now().timestamp()) // half_ttl


def feed_etag(request, *, head: Iterable, entries: Iterable = ()) -> str:
    """ETag (unquoted) for a feed response.

    `head` holds the feed-level inputs (ids, timestamps, page number);
    `entries` is a sequence of `(pk, touched_at)` pairs for the page.
    """
    entries = list(entries)
    parts = [ETAG_VERSION, request_language(request), *head, *(f"{pk}@{touched}" for pk, touched in entries)]

    user = request.user
    if user.is_authenticated:
        parts.append(f"user:{user.pk}")
        licenses = sorted(
            str(pk)
            for pk in License.objects.filter(
                user=user,
                entry_id__in=[pk for pk, _ in entries],
                state__in=LICENSED_STATES,
            ).values_list("pk", flat=True)
        )
        if licenses:
            parts.extend(f"license:{pk}" for pk in licenses)
            parts.append(f"bucket:{_token_bucket()}")
    else:
        parts.append("anonymous")

    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def not_modified(request, etag: str) -> Optional:
    """A `304` when the client's `If-None-Match` matches, else `None`."""
    response = get_conditional_response(request, etag=quote_etag(etag))
    if response is not None:
        _patch_headers(request, response, etag)
    return response


def finalize(request, response, etag: str):
    """Attach the validator and the cache headers to a rendered feed."""
    _patch_headers(request, response, etag)
    return response


def _patch_headers(request, response, etag: str):
    response["ETag"] = quote_etag(etag)
    # The body depends on who asks and in which language. `no-cache` keeps
    # every reuse behind a revalidation, which the ETag makes cheap.
    patch_vary_headers(response, ("Authorization", "Accept-Language"))
    if request.user.is_authenticated:
        patch_cache_control(response, private=True, no_cache=True)
    else:
        patch_cache_control(response, no_cache=True)

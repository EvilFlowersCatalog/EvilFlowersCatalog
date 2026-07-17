"""
Capability-token scopes for the Readium app (IP-009 Phase 4 D1).

Scopes are plain string constants registered here so callsites import
the symbol rather than passing magic strings. Each scope has a fixed
mode (single-use vs multi-use) and a configurable TTL.

Add new scopes by appending to this module — `CapabilityTokenService`
is scope-agnostic.
"""

from django.conf import settings

# Short TTL (~60s by default), redeemable repeatedly within that window.
# Reader apps redeem via `GET .../{id}.lcpl?token=…`.
#
# NOT single-use: Thorium fetches the URL twice — a throwaway GET to sniff
# `Content-Type`, then the real download. A single-use token is consumed by
# the sniff and the download 401s ("publicationDocument not imported on db").
# Keep the TTL short rather than reaching for single-use again.
LCPL_DOWNLOAD = "lcpl_download"

# Multi-use within TTL (peek-able), ~30 min. Issued automatically when
# `LicenseSerializer.Base.download_url` is rendered inside an OPDS
# feed context. Lets a reading app fetch the same `.lcpl` multiple
# times during the feed window without re-minting.
LCPL_FEED_DOWNLOAD = "lcpl_feed_download"


def lcpl_download_ttl() -> int:
    return settings.EVILFLOWERS_CAPABILITY_TOKEN_LCPL_TTL_SECONDS


def lcpl_feed_download_ttl() -> int:
    return settings.EVILFLOWERS_CAPABILITY_TOKEN_LCPL_FEED_TTL_SECONDS

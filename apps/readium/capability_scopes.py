"""
Capability-token scopes for the Readium app (IP-009 Phase 4 D1).

Scopes are plain string constants registered here so callsites import
the symbol rather than passing magic strings. Each scope has a fixed
mode (single-use vs multi-use) and a configurable TTL.

Add new scopes by appending to this module — `CapabilityTokenService`
is scope-agnostic.
"""

from django.conf import settings

# Single-use, ~60s TTL. Issued by `POST /readium/v1/licenses/{id}/download-tokens`.
# Reader apps redeem once via `GET .../{id}.lcpl?token=…`.
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

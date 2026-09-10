"""The `evilflowers://` URI space.

MCP resources are addressed by URI, and tool results carry `resource_link`
blocks pointing into the same space. Keeping the scheme in one module means a
link a tool emits and a URI `resources/read` accepts can never drift apart.

    evilflowers://catalog/<uuid>
    evilflowers://entry/<uuid>
    evilflowers://feed/<uuid>

These are identifiers, not fetchable URLs — `resources/read` resolves them
through the same access control as the equivalent tool. An HTTP URL a human can
open (cover image, acquisition download, OPDS feed) is a separate field on the
projection.
"""

from typing import NamedTuple, Optional
from uuid import UUID

SCHEME = "evilflowers"

CATALOG = "catalog"
ENTRY = "entry"
FEED = "feed"

KINDS = (CATALOG, ENTRY, FEED)


class ResourceRef(NamedTuple):
    kind: str
    identifier: str


def build(kind: str, identifier) -> str:
    return f"{SCHEME}://{kind}/{identifier}"


def catalog_uri(identifier) -> str:
    return build(CATALOG, identifier)


def entry_uri(identifier) -> str:
    return build(ENTRY, identifier)


def feed_uri(identifier) -> str:
    return build(FEED, identifier)


def parse(uri: str) -> Optional[ResourceRef]:
    """Split a URI into (kind, uuid), or `None` if it is not one of ours.

    Strict on purpose: an unrecognised shape returns `None` rather than a
    partially-parsed guess, so `resources/read` reports "unknown URI" instead of
    querying for something the caller did not ask for.
    """
    if not isinstance(uri, str) or not uri.startswith(f"{SCHEME}://"):
        return None

    remainder = uri[len(SCHEME) + 3 :]
    kind, separator, identifier = remainder.partition("/")
    if not separator or kind not in KINDS:
        return None

    try:
        return ResourceRef(kind=kind, identifier=str(UUID(identifier)))
    except ValueError:
        return None


__all__ = [
    "CATALOG",
    "ENTRY",
    "FEED",
    "KINDS",
    "SCHEME",
    "ResourceRef",
    "build",
    "catalog_uri",
    "entry_uri",
    "feed_uri",
    "parse",
]

"""The `evilflowers://` URI space.

MCP resources are addressed by URI, and tool results carry `resource_link`
blocks pointing into the same space. Keeping the scheme in one module means a
link a tool emits and a URI `resources/read` accepts can never drift apart.

    evilflowers://catalog/<uuid>
    evilflowers://entry/<uuid>
    evilflowers://entry/<uuid>/outline              the publication's sections, each with its locator
    evilflowers://entry/<uuid>/locator/<key>        a paragraph, section or figure, run-independent (#171)
    evilflowers://feed/<uuid>

These are identifiers, not fetchable URLs — `resources/read` resolves them
through the same access control as the equivalent tool. An HTTP URL a human can
open (cover image, acquisition download, OPDS feed) is a separate field on the
projection.
"""

import re
from typing import NamedTuple, Optional
from uuid import UUID

SCHEME = "evilflowers"

CATALOG = "catalog"
ENTRY = "entry"
FEED = "feed"

KINDS = (CATALOG, ENTRY, FEED)

#: Parts of an entry (`evilflowers://entry/<uuid>/<part>[/<key>]`).
OUTLINE = "outline"
LOCATOR = "locator"

#: A locator key: base32 (paragraphs, regions) or hex (sections), lower case.
_LOCATOR_KEY = re.compile(r"^[a-z0-9]{8,40}$")


class ResourceRef(NamedTuple):
    kind: str
    identifier: str
    #: `outline` or `locator` for a part of an entry; None for the entry itself.
    part: Optional[str] = None
    #: The locator key of a `locator` part.
    key: Optional[str] = None


def build(kind: str, identifier) -> str:
    return f"{SCHEME}://{kind}/{identifier}"


def outline_uri(entry_id) -> str:
    return f"{entry_uri(entry_id)}/{OUTLINE}"


def locator_uri(entry_id, key: str) -> str:
    return f"{entry_uri(entry_id)}/{LOCATOR}/{key}"


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

    identifier, _slash, part = identifier.partition("/")
    try:
        identifier = str(UUID(identifier))
    except ValueError:
        return None
    if not part:
        return ResourceRef(kind=kind, identifier=identifier)
    if kind != ENTRY:
        return None
    if part == OUTLINE:
        return ResourceRef(kind=kind, identifier=identifier, part=OUTLINE)
    name, _slash, key = part.partition("/")
    if name == LOCATOR and _LOCATOR_KEY.match(key):
        return ResourceRef(kind=kind, identifier=identifier, part=LOCATOR, key=key)
    return None


__all__ = [
    "CATALOG",
    "ENTRY",
    "FEED",
    "KINDS",
    "LOCATOR",
    "OUTLINE",
    "SCHEME",
    "ResourceRef",
    "build",
    "catalog_uri",
    "entry_uri",
    "feed_uri",
    "locator_uri",
    "outline_uri",
    "parse",
]

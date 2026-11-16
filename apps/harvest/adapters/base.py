"""What every adapter offers, and the one hardened XML parser they all use.

Source XML is untrusted: no entity expansion, no DTDs, no network, no huge trees. pymarc's own MARCXML
reader is SAX-based and not hardened, so MARCXML is parsed here and converted element by element
(`apps.harvest.adapters.marc.record_from_element`).
"""

from __future__ import annotations

from typing import Iterator, Optional, Protocol

from lxml import etree

from apps.harvest.records import SourceRecord

USER_AGENT = "EvilFlowersCatalog-harvester"


class HarvestError(Exception):
    """The source answered something a run cannot go on with (HTTP error, OAI error, unreadable file)."""


class Adapter(Protocol):
    #: Where the next incremental run starts; set once `records()` has been read to the end.
    cursor: Optional[str]
    #: Whether a complete `records()` pass lists every live record of the source (a full OAI harvest does):
    #: only then are records not seen treated as deleted.
    exhaustive: bool

    def records(self) -> Iterator[SourceRecord]: ...


def parse_xml(content: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False
    )
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as error:
        raise HarvestError(f"Unreadable XML: {error}") from error
    if root is None:
        raise HarvestError("Empty XML document.")
    return root


def local(element) -> str:
    """Tag without namespace (`{ns}record` → `record`); comments and PIs have none."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def children(element, name: str) -> list:
    return [child for child in element if local(child) == name]


def child(element, name: str):
    return next((item for item in element if local(item) == name), None)


def text(element) -> Optional[str]:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


__all__ = ["Adapter", "HarvestError", "USER_AGENT", "child", "children", "local", "parse_xml", "text"]

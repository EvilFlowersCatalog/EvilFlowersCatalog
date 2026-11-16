"""The connector contract and registry (IP-034): which systems a `HarvestSource.adapter` may name.

Like IP-031's `JobDefinition` registry, the app that knows a system implements `Connector` and registers it;
`apps/harvest` never imports it, and `HarvestSource.adapter` is a plain string validated against `names()`, so a
new connector needs no migration here.

    from apps.harvest.contract import Connector, PushResult, StaleVersion, registry

    @registry.register
    class ExampleConnector(Connector):
        name = "example"
        title = "Example repository"
        system_keys = ("example_pid",)

        def reader(self, source, *, full=False): ...           # read (required)
        def fetch(self, source, record_id): ...                 # read (required)
        def push(self, source, record_id, record, expected_version): ...   # write (optional)
        def validate(self, source, record): ...                 # validate (optional)
        def record_url(self, source, record_id): ...            # link (optional)

Capabilities are read from what a subclass overrides: `read` always; `write` when `push` is overridden and
`writable()` says so (a connector without its credentials reports itself read-only); `validate`, `link` when
those are overridden.

Connection details beyond `base_url`, `metadata_prefix` and `set_spec` live in `HarvestSource.config`; secrets
never do: a connector reads tokens from settings, named in its config (`{"token_setting": "EVILFLOWERS_…"}`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Optional, Protocol

from apps.harvest.records import SourceRecord

#: Config keys that look like a credential. A connector names the setting that holds it instead (`*_setting`).
SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|credential)", re.IGNORECASE)

CAPABILITIES = ("read", "write", "validate", "link")


class Reader(Protocol):
    """One pass over a source: `records()` yields `SourceRecord`s (deleted ones with `deleted=True`); after the
    last one `cursor` holds where the next incremental pass starts. `exhaustive`: a complete pass lists every
    live record, so records not seen are gone. `probe()` answers the admin API's connection test."""

    cursor: Optional[str]
    exhaustive: bool

    def records(self) -> Iterator[SourceRecord]: ...

    def probe(self) -> dict: ...


class StaleVersion(Exception):
    """`push` refused: the remote record changed since `expected_version` (optimistic locking)."""


@dataclass
class PushResult:
    #: The remote record's id (new for a creation): stored in `identifiers[source.system_key]`.
    record_id: str
    #: The remote's version after the write, opaque (e.g. its last-update time).
    version: Optional[str] = None
    #: Further `Entry.identifiers` keys the connector owns (e.g. a second internal id); allowed keys only.
    identifiers: dict[str, str] = field(default_factory=dict)


class Connector:
    """Base of every connector. Override what the system supports; the class attributes describe it."""

    #: Registry name, stored in `HarvestSource.adapter`.
    name: ClassVar[str]
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: JSON Schema of `HarvestSource.config` for the portal's source form; None: no connector config.
    config_schema: ClassVar[Optional[dict]] = None
    needs_base_url: ClassVar[bool] = False
    needs_upload: ClassVar[bool] = False
    #: False: `harvest.run` jobs do not run it (records arrive another way).
    runnable: ClassVar[bool] = True
    #: `Entry.identifiers` keys the connector owns and a source may link through (besides configured systems).
    system_keys: ClassVar[tuple[str, ...]] = ()

    # -- read (required) -----------------------------------------------------------------------------------

    def reader(self, source, *, full: bool = False) -> Reader:
        """A pass over the source from `source.cursor` (`full`: from the beginning)."""
        raise NotImplementedError

    def fetch(self, source, record_id: str) -> Optional[SourceRecord]:
        """One record as the remote holds it now, with `version`; None or `deleted=True` when it is gone."""
        raise NotImplementedError

    def test(self, source) -> dict:
        """`{identify, metadata_formats, sets, sample, errors}` for `POST harvest/sources/{id}/test`."""
        return self.reader(source).probe()

    # -- write (optional) ----------------------------------------------------------------------------------

    def push(
        self, source, record_id: Optional[str], record: SourceRecord, expected_version: Optional[str]
    ) -> PushResult:
        """Write `record` to the remote: update `record_id` if `expected_version` is still current (else raise
        `StaleVersion`), or create a new record when `record_id` is None."""
        raise NotImplementedError

    def writable(self, source=None) -> bool:
        """Whether `push` can work now (credentials configured); only asked when `push` is implemented."""
        return True

    # -- validate, link (optional) -------------------------------------------------------------------------

    def validate(self, source, record: SourceRecord) -> list[str]:
        """Problems that should stop `record` from being published remotely; empty when it is fine."""
        return []

    def record_url(self, source, record_id: str) -> Optional[str]:
        """The remote's landing page of a record."""
        return None

    def validate_config(self, config: dict) -> list[str]:
        return []

    # -- capabilities --------------------------------------------------------------------------------------

    def _overrides(self, method: str) -> bool:
        return getattr(type(self), method) is not getattr(Connector, method)

    def can_write(self, source=None) -> bool:
        return self._overrides("push") and self.writable(source)

    def capabilities(self, source=None) -> list[str]:
        result = ["read"]
        if self.can_write(source):
            result.append("write")
        if self._overrides("validate"):
            result.append("validate")
        if self._overrides("record_url"):
            result.append("link")
        return result


class ConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, Connector] = {}

    def register(self, connector):
        """Register a `Connector` subclass (usable as a class decorator) or instance."""
        instance = connector() if isinstance(connector, type) else connector
        existing = self._connectors.get(instance.name)
        if existing is not None and type(existing) is not type(instance):
            raise ValueError(f"Harvest connector '{instance.name}' is already registered")
        self._connectors[instance.name] = instance
        return connector

    def unregister(self, name: str) -> None:
        self._connectors.pop(name, None)

    def get(self, name: str) -> Optional[Connector]:
        return self._connectors.get(name)

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def all(self) -> list[Connector]:
        return [self._connectors[name] for name in self.names()]

    def system_keys(self) -> set[str]:
        return {key for connector in self._connectors.values() for key in connector.system_keys}


registry = ConnectorRegistry()


def config_problems(connector: Optional[Connector], config: dict) -> list[str]:
    problems = [
        f"`config.{key}` looks like a credential; put it in a setting and name the setting (`{key}_setting`)."
        for key in config
        if SECRET_KEY.search(key) and not key.endswith("_setting")
    ]
    if connector is not None:
        problems.extend(connector.validate_config(config))
    return problems


def describe(connector: Connector) -> dict[str, Any]:
    return {
        "name": connector.name,
        "title": connector.title,
        "description": connector.description,
        "capabilities": connector.capabilities(),
        "config_schema": connector.config_schema,
        "needs_base_url": connector.needs_base_url,
        "needs_upload": connector.needs_upload,
        "runnable": connector.runnable,
    }


__all__ = [
    "CAPABILITIES",
    "Connector",
    "ConnectorRegistry",
    "PushResult",
    "Reader",
    "StaleVersion",
    "config_problems",
    "describe",
    "registry",
]

"""Connectors turn a source into `SourceRecord`s (IP-034). The built-in ones (OAI-PMH, MARC files) register
themselves on import; other apps register theirs through `apps.harvest.contract.registry`."""

from apps.harvest.adapters.base import Adapter, HarvestError
from apps.harvest.adapters.registry import Connector, PushResult, StaleVersion, registry
from apps.harvest.adapters import marc_file, oai  # noqa: F401,E402  (registration side effect)

OAI_PMH = oai.NAME
MARC_FILE = marc_file.NAME


def connector_for(source) -> Connector:
    connector = registry.get(source.adapter)
    if connector is None:
        raise HarvestError(f"Unknown harvest connector '{source.adapter}'.")
    return connector


def adapter_for(source, *, full: bool = False) -> Adapter:
    """One pass over the source (the connector's `reader`)."""
    return connector_for(source).reader(source, full=full)


__all__ = [
    "Adapter",
    "Connector",
    "HarvestError",
    "MARC_FILE",
    "OAI_PMH",
    "PushResult",
    "StaleVersion",
    "adapter_for",
    "connector_for",
    "registry",
]

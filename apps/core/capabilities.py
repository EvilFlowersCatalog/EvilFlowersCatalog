"""What this deployment can do, whatever the settings say (#212).

A capability is declared, not coded: the Django settings it needs (each must be truthy), the app that must be
installed, and at most one runtime `probe` for what configuration cannot tell (the library graph is built). The
same idea as `Module.requires` in `apps.console.modules`.

The portal hides a feature whose capability is false. A capability not listed here is unknown to the portal, so
the feature stays available: adding one here is how a deployment learns to hide a feature it cannot serve.

Read through `GET /api/v1/settings?include=capabilities`.
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from django.apps import apps as django_apps
from django.conf import settings


def _graph_built() -> bool:
    from apps.graph import age

    return age.available()  # extension installed and the graph exists; cached per process for a minute


@dataclass(frozen=True)
class Capability:
    key: str
    description: str
    #: Django settings that must all be truthy.
    settings: Tuple[str, ...] = ()
    #: An app that must be in INSTALLED_APPS (a feature that is not mounted is not there).
    app: Optional[str] = None
    #: A runtime check, called only when the declared requirements hold.
    probe: Optional[Callable[[], bool]] = None

    def available(self) -> bool:
        if self.app and not django_apps.is_installed(self.app):
            return False
        if not all(getattr(settings, name, None) for name in self.settings):
            return False
        return self.probe() if self.probe else True


CAPABILITIES: Tuple[Capability, ...] = (
    Capability(
        "assistant",
        "The assistant is mounted and has a model.",
        settings=("EVILFLOWERS_ASSISTANT_ENABLED", "EVILFLOWERS_ASSISTANT_OLLAMA_MODEL"),
    ),
    Capability(
        "extraction", "Publications are extracted into passages.", settings=("EVILFLOWERS_EXTRACTION_ENABLED",)
    ),
    Capability(
        "hybrid_search", "Search adds embeddings to the lexical query.", settings=("EVILFLOWERS_EMBEDDING_URL",)
    ),
    Capability("lcp", "Readium LCP lending has a license server.", settings=("EVILFLOWERS_READIUM_LCPSV_URL",)),
    Capability("graph", "The library graph is built.", app="apps.graph", probe=_graph_built),
    Capability("harvest", "Harvest and sync connectors are mounted.", app="apps.harvest"),
)


def states() -> Dict[str, bool]:
    return {item.key: item.available() for item in CAPABILITIES}


__all__ = ["CAPABILITIES", "Capability", "states"]

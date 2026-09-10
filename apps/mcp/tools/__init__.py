"""Importing this package registers every tool.

Each module below calls `@registry.tool(...)` at import time, so the registry is
populated as a side effect of this import. `apps.mcp.views` imports `registry`
from here — never from `apps.mcp.registry` directly — so it cannot observe a
half-populated catalogue.
"""

from apps.mcp.registry import registry
from apps.mcp.tools import (  # noqa: F401  (registration side effect)
    catalogs,
    categories,
    entries,
    feeds,
    library,
)

__all__ = ["registry"]

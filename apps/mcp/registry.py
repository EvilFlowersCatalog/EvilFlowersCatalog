"""The tool catalogue that `tools/list` advertises and `tools/call` dispatches to.

A tool is a name, JSON Schemas for its arguments and result, an access level, and
a handler with the signature `handler(request, arguments) -> dict`. The handler
receives the live Django request (for `request.user` and `build_absolute_uri`)
and returns a JSON-serialisable object built from primitives only — the
transport does not own a custom JSON encoder.

**Access is declared, not remembered.** Every tool states its level; the server
enforces it before the handler runs. A handler cannot forget to check, because
checking is not the handler's job. What the handler *does* still own is
object-level permission — "may this user manage *this* catalog" — because only
it knows which object is involved.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

ToolHandler = Callable[..., dict]


class ToolAccess(str, Enum):
    """What a caller must be before a tool will run at all."""

    #: Anonymous callers welcome. Catalog access control still scopes the
    #: result — "public" is about the session, never about the data.
    PUBLIC = "public"
    #: Needs an authenticated session. Used by tools that answer *about the
    #: caller* (`get_my_shelf`), where an anonymous session has no meaning.
    USER = "user"
    #: Needs an authenticated session *and* the write surface enabled
    #: (`EVILFLOWERS_MCP_ALLOW_WRITE`). The handler must additionally check
    #: object-level permission with `has_object_permission`.
    WRITE = "write"


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: ToolHandler
    access: ToolAccess = ToolAccess.PUBLIC
    #: Advertised as `outputSchema`. When present, `structuredContent` must
    #: conform — clients are entitled to validate it.
    output_schema: Optional[dict] = None
    #: True for tools that remove or overwrite data. Clients use the hint to
    #: decide whether to ask the human first.
    destructive: bool = False
    #: False for create-style tools, where calling twice makes two things.
    idempotent: bool = True

    @property
    def writes(self) -> bool:
        return self.access is ToolAccess.WRITE

    def descriptor(self) -> dict:
        """The `tools/list` entry for this tool.

        `title`, `outputSchema` and `annotations` arrived in later protocol
        revisions; older clients ignore unknown keys, so one descriptor serves
        every revision we speak.
        """
        descriptor = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": not self.writes,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                # Everything is answered from this catalog's own database.
                "openWorldHint": False,
            },
            "_meta": {"evilflowers/access": self.access.value},
        }
        if self.output_schema is not None:
            descriptor["outputSchema"] = self.output_schema
        return descriptor


@dataclass
class ToolRegistry:
    _tools: Dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"MCP tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        *,
        name: str,
        title: str,
        description: str,
        input_schema: dict,
        output_schema: Optional[dict] = None,
        access: ToolAccess = ToolAccess.PUBLIC,
        destructive: bool = False,
        idempotent: bool = True,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            self.register(
                Tool(
                    name=name,
                    title=title,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    handler=handler,
                    access=access,
                    destructive=destructive,
                    idempotent=idempotent,
                )
            )
            return handler

        return decorator

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def available(self, *, write_enabled: bool) -> List[Tool]:
        """Tools this deployment offers, in a stable order.

        With writes disabled the management tools are not merely refused, they
        are never advertised — a model cannot plan around a capability it was
        never told about, and that is the behaviour we want from a kill switch.
        """
        tools: Iterable[Tool] = self._tools.values()
        if not write_enabled:
            tools = (tool for tool in tools if not tool.writes)
        return sorted(tools, key=lambda tool: tool.name)

    def descriptors(self, *, write_enabled: bool) -> List[dict]:
        return [tool.descriptor() for tool in self.available(write_enabled=write_enabled)]


registry = ToolRegistry()


__all__ = ["Tool", "ToolAccess", "ToolRegistry", "registry"]

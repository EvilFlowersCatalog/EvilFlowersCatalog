from enum import Enum
from typing import Dict, TypedDict, NamedTuple, List, Optional


class InstanceDetails(TypedDict):
    constructor: str
    kwargs: dict[str, str]
    args: list[str]


class ParameterLocation(str, Enum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


ExtractionResult = NamedTuple(
    "ExtractionResult",
    [
        ("returns", List[InstanceDetails]),
        ("raises", List[InstanceDetails]),
        ("filters", List[str]),
        ("form", Optional[str]),
    ],
)

OPENAPI_TYPES: Dict[str, Dict[str, str]] = {
    "str": {"type": "string"},
    "bool": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "int": {"type": "integer", "format": "int32"},
    "datetime": {"type": "string", "format": "date-time"},
}

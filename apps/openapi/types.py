import importlib
from enum import Enum
from typing import Dict, TypedDict, NamedTuple, List, Optional, Type

from pydantic import BaseModel, Field


class InstanceDetails(TypedDict):
    constructor: str
    kwargs: dict[str, str]
    args: list[str]


class OpenApiInfo(TypedDict):
    title: str
    description: str
    version: str


class ParameterLocation(str, Enum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class OpenApiBaseModel(BaseModel):
    class Config:
        arbitrary_types_allowed = True
        use_enum_values = True

    @staticmethod
    def _load_class(class_path: str) -> Type:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls

    @staticmethod
    def _find_all_subclasses(needle: Type) -> Dict[str, Type]:
        subclasses: set = set()
        to_process: List[Type] = [needle]
        while to_process:
            parent = to_process.pop()
            for child in parent.__subclasses__():
                if child not in subclasses:
                    subclasses.add(child)
                    to_process.append(child)
        return {cls.__qualname__: cls for cls in subclasses}


class OpenApiParameter(OpenApiBaseModel):
    name: str
    location: ParameterLocation = Field(serialization_alias="in")
    description: Optional[str]
    required: bool = Field(default=True)
    schema_type: Dict = Field(serialization_alias="schema")


ExtractionResult = NamedTuple(
    "ExtractionResult", [("returns", List[InstanceDetails]), ("raises", List[InstanceDetails]), ("filters", List[str])]
)

OPENAPI_TYPES: Dict[str, Dict[str, str]] = {
    "str": {"type": "string"},
    "uuid": {"type": "string", "format": "uuid"},
    "int": {"type": "integer", "format": "int32"},
}

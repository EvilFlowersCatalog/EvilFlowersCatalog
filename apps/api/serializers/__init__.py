from copy import copy
from typing import Any

from pydantic import BaseModel, ConfigDict


class Serializer(BaseModel):
    class Config:
        from_attributes = True

        @staticmethod
        def model_title_generator(model):
            return model.__qualname__

        @staticmethod
        def json_schema_extra(schema: dict[str, Any]) -> None:
            """
            OpenAPI does not allow to use 'null' as type
            """
            for name, prop in schema.get("properties", {}).items():
                if "anyOf" in prop:
                    result = copy(prop)
                    result["anyOf"] = []
                    for i in prop.get("anyOf", []):
                        if i.get("type") == "null":
                            result["nullable"] = True
                        else:
                            result["anyOf"].append(i)

                    schema["properties"][name] = result

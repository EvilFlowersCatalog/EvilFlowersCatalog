import re

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import CoreModeRef, DefsRef, GenerateJsonSchema, JsonSchemaValue


def schema_name(model: type) -> str:
    """A model's or enum's name in the protocol: `apps.core.errors.DetailType` -> `apps__core__errors__DetailType`.

    Fully qualified on purpose: `EntrySerializer.Base` and `RunSerializer.Base` are both `Base` to pydantic.
    """
    return f"{model.__module__}.{model.__qualname__}".replace(".", "__")


class ProtocolJsonSchema(GenerateJsonSchema):
    """JSON Schema of the protocol (OpenAPI 3.1 `components`, evilflowers-protocol, the Python client).

    Pydantic names a definition by its bare class name when that is unique *within one generation*. The OpenAPI
    document generates each serializer on its own and merges the results, so `Base` from `JobSerializer` and `Base`
    from `MessageSerializer` met only there, and every `$ref` to `Base` pointed at whichever came first. Here every
    definition is named by `schema_name`, the same name the serializer has as a top-level component.
    """

    def get_defs_ref(self, core_mode_ref: CoreModeRef) -> DefsRef:
        # The core ref is `module.Qualname:<id>`, generic arguments in brackets with ids of their own; drop the ids
        # and qualify: `apps.core.errors.DetailType:4402` -> `apps__core__errors__DetailType`.
        core_ref, _mode = core_mode_ref
        qualified = "".join(part.rsplit(":", 1)[0] for part in re.split(r"([\][,])", core_ref))
        defs_ref = DefsRef(self.normalize_name(qualified))
        # The only candidate, so pydantic's final remapping keeps it as is.
        self._prioritized_defsref_choices[defs_ref] = [defs_ref]
        return defs_ref


class Serializer(BaseModel):
    """Base of every response model. Its JSON Schema is the protocol (OpenAPI 3.1, evilflowers-protocol).

    * `json_schema_mode_override="serialization"`: a serializer describes what the API *answers*, so its schema
      includes `@computed_field`s and follows field serializers, wherever it is asked for (OpenAPI, MCP, tests).
    * The schema is plain JSON Schema 2020-12 as pydantic writes it (`anyOf: [..., {"type": "null"}]` for
      optional values); OpenAPI 3.1 takes it unchanged, and the generated Python client reads it back as `X | None`.
    * `protocol_schema()` names nested definitions as the protocol does (see `ProtocolJsonSchema`).
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_mode_override="serialization",
        model_title_generator=lambda model: model.__qualname__,
    )

    @classmethod
    def protocol_schema(cls, ref_template: str = "#/components/schemas/{model}") -> JsonSchemaValue:
        return cls.model_json_schema(ref_template=ref_template, schema_generator=ProtocolJsonSchema)

    @classmethod
    def protocol_name(cls) -> str:
        return schema_name(cls)


__all__ = ["ProtocolJsonSchema", "Serializer", "schema_name"]

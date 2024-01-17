from pydantic import BaseModel, ConfigDict


class Serializer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

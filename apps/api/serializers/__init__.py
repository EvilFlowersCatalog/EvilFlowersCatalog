from pydantic import BaseModel, ConfigDict


class Serializer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # myclass['my_attr']
    def __get__(self):
        ...

    # myclass['my_attr'] = kkt
    def __set__(self):
        ...

    # entry.
    def __getattr__(self, item):
        ...

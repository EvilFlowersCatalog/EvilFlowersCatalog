from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .link import Link


class Subject(BaseModel):
    """RWPM Subject (category/classification).

    https://readium.org/webpub-manifest/schema/subject.schema.json
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    sort_as: Optional[str] = Field(None, alias="sortAs")
    scheme: Optional[str] = None
    code: Optional[str] = None
    links: Optional[list[Link]] = None

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .link import Link


class Contributor(BaseModel):
    """RWPM Contributor (author, translator, editor, publisher, etc.).

    https://readium.org/webpub-manifest/schema/contributor.schema.json
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    identifier: Optional[str] = None
    sort_as: Optional[str] = Field(None, alias="sortAs")
    role: Optional[list[str]] = None
    links: Optional[list[Link]] = None
    position: Optional[float] = None

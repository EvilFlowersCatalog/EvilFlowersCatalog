from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .contributor import Contributor


class BelongsTo(BaseModel):
    """RWPM BelongsTo (series/collection membership).

    https://readium.org/webpub-manifest/schema/belongs-to.schema.json
    """

    model_config = ConfigDict(populate_by_name=True)

    series: Optional[list[Contributor]] = None
    collection: Optional[list[Contributor]] = None

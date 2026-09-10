from pydantic import BaseModel


class Price(BaseModel):
    """OPDS 2.0 Price.

    https://drafts.opds.io/opds-2.0#4-acquisition-objects
    """

    currency: str
    value: float

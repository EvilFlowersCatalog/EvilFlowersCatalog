from typing import TypedDict, Optional
from uuid import UUID


class ModifierContext(TypedDict):
    id: Optional[UUID]
    user_id: Optional[UUID]
    username: Optional[str]
    title: Optional[str]
    generated_at: Optional[str]
    url: Optional[str]
    instance: Optional[str]
    contact: Optional[str]
    authors: Optional[str]


class ModifierException(Exception):
    pass


class InvalidPage(ModifierException):
    pass

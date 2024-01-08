from pydantic import BaseModel

from apps.api.serializers.users import UserSerializer


class TokenSerializer:
    class Access(BaseModel):
        access_token: str
        refresh_token: str
        user: UserSerializer.Detailed

    class Refresh(BaseModel):
        access_token: str

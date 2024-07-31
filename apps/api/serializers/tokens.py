from apps.api.serializers import Serializer
from apps.api.serializers.users import UserSerializer


class TokenSerializer:
    class Access(Serializer):
        access_token: str
        refresh_token: str
        user: UserSerializer.Detailed

    class Refresh(Serializer):
        access_token: str

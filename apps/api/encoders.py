import decimal
from enum import Enum
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.paginator import Page
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import QuerySet
from django.utils.translation import gettext as _


class ApiJSONEncoder(DjangoJSONEncoder):
    def __init__(self, **kwargs):
        if 'serializer' in kwargs:
            self._serializer = kwargs.get('serializer', None)
            del kwargs['serializer']
        super().__init__(**kwargs)

    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, models.Model):
            if self._serializer:
                return self._serializer(o).dict()
            else:
                raise RuntimeError(_('Serializer non specified.'))
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, Page):
            return o.object_list
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, QuerySet):
            return list(o)
        if isinstance(o, set):
            return list(o)
        if isinstance(o, ValidationError):
            return o.message
        return DjangoJSONEncoder.default(self, o)


__all__ = [
    'ApiJSONEncoder'
]

import random
from django.db import models

from apps.core.querysets.base import BaseQuerySet


class BaseManager(models.Manager):
    def __init__(self, *args, **kwargs):
        self._alive_only = kwargs.pop('alive_only', True)
        super(BaseManager, self).__init__(*args, **kwargs)

    def hard_delete(self):
        return self.get_queryset().hard_delete()

    def random(self):
        return random.choice(self.all())

    def get_queryset(self):
        if self._alive_only:
            return BaseQuerySet(self.model).alive()
        return BaseQuerySet(self.model)

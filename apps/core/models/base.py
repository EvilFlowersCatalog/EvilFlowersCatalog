import uuid

from django.db import models
from django.db.models.functions import Now


class BaseModel(models.Model):
    class Meta:
        abstract = True

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    #: Fields that name a row, first match wins (IP-035: admin links, raw-id widgets, autocomplete).
    LABEL_FIELDS = ("title", "username", "name", "key", "slug", "url_name", "code", "term")

    def __str__(self):
        # Models that define their own `__str__` keep it. A JSON/dict title (feeds) is not a label.
        for name in self.LABEL_FIELDS:
            if name in self.__dict__:
                value = self.__dict__[name]
                if isinstance(value, str) and value:
                    return value
        return f"{self._meta.verbose_name} {str(self.pk)[:8]}"

    def update(self, data: dict):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.save()

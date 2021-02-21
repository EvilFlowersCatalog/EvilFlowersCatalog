import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone
from django.utils.deconstruct import deconstructible

from apps.core.managers.base import BaseManager


class BaseModel(models.Model):
    class Meta:
        abstract = True

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    objects = BaseManager()
    all_objects = BaseManager(alive_only=False)

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save()

    def hard_delete(self):
        super(BaseModel, self).delete()

    def update(self, data: dict):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.save()


@deconstructible
class PrivateFileStorage(FileSystemStorage):
    def __init__(self):
        super(PrivateFileStorage, self).__init__(location=settings.PRIVATE_DIR)

    def __eq__(self, other):
        return self.subdir == other.subdir


private_storage = PrivateFileStorage()

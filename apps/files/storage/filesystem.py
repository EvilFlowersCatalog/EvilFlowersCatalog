import os
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage as DjangoFileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class FileSystemStorage(DjangoFileSystemStorage):
    def __init__(self):
        data_path = Path(settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR)

        os.makedirs(settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR, exist_ok=True)

        if not data_path.exists():
            raise ImproperlyConfigured(f"Path EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR({data_path}) does not exists")

        super(FileSystemStorage, self).__init__(location=settings.EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR)

    def __eq__(self, other):
        return self.subdir == other.subdir

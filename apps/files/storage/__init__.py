from django.conf import settings
from django.core.files.storage import Storage
from django.utils.module_loading import import_string


def get_storage() -> Storage:
    return import_string(settings.STORAGE_DRIVER)()

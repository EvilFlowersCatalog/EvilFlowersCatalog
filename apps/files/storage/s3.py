from io import BytesIO
from mimetypes import guess_type

import minio
from django.conf import settings
from django.core.files import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class S3Storage(Storage):
    def __init__(self):
        self._client = minio.Minio(
            settings.EVILFLOWERS_STORAGE_S3_HOST,
            access_key=settings.EVILFLOWERS_STORAGE_S3_ACCESS_KEY,
            secret_key=settings.EVILFLOWERS_STORAGE_S3_SECRET_KEY,
            secure=settings.EVILFLOWERS_STORAGE_S3_SECURE
        )

    def _open(self, name, mode="rb"):
        return File(self._client.get_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name))

    def _save(self, name, content):
        content_type = getattr(content, 'content_type', None)

        if not content_type:
            content_type = guess_type(name)[0]

        content = BytesIO(content.read())
        content.seek(0)

        s3_object = self._client.put_object(
            settings.EVILFLOWERS_STORAGE_S3_BUCKET,
            name,
            content,
            content.getbuffer().nbytes,
            content_type=content_type
        )
        return s3_object.object_name

    def path(self, name):
        raise NotImplementedError("This backend doesn't support absolute paths.")

    def delete(self, name):
        self._client.remove_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name)

    def exists(self, name):
        try:
            self._client.stat_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name)
            return True
        except Exception:
            return False

    def listdir(self, path):
        raise NotImplementedError("This backend doesn't support directories.")

    def size(self, name):
        s3_object = self._client.stat_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name)
        return s3_object.size

    def url(self, name):
        raise NotImplementedError("This backend doesn't support public URLs.")

    def get_accessed_time(self, name):
        raise NotImplementedError("This backend doesn't support accessed time.")

    def get_created_time(self, name):
        s3_object = self._client.stat_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name)
        return s3_object.last_modified

    def get_modified_time(self, name):
        s3_object = self._client.stat_object(settings.EVILFLOWERS_STORAGE_S3_BUCKET, name)
        return s3_object.last_modified

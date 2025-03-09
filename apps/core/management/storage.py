import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from minio import Minio


class BackupDestinationStrategy:
    """Abstract destination strategy."""

    def move(self, local_file: str) -> str:
        raise NotImplementedError("Subclasses must implement save().")


class LocalDestinationStrategy(BackupDestinationStrategy):
    def __init__(self, destination: str):
        self.destination = Path(destination)

    def move(self, local_file: str) -> str:
        # Ensure destination directory exists
        if not self.destination.parent.exists():
            os.makedirs(self.destination.parent)
        os.rename(local_file, str(self.destination))
        return str(self.destination)


class S3DestinationStrategy(BackupDestinationStrategy):
    def __init__(self, destination: str):
        """
        destination: a Path instance whose string is an S3 URL,
        e.g. s3://bucket/folder/backup.dump
        """
        parsed = urlparse(str(destination))
        self.bucket = parsed.netloc
        self.object_name = parsed.path.lstrip("/")  # Remove leading '/'

        self.client = Minio(
            settings.EVILFLOWERS_BACKUP_S3_HOST,
            access_key=settings.EVILFLOWERS_BACKUP_S3_ACCESS_KEY,
            secret_key=settings.EVILFLOWERS_BACKUP_S3_SECRET_KEY,
            secure=settings.EVILFLOWERS_BACKUP_S3_SECURE,
        )

    def move(self, local_file: str) -> Optional[str]:
        try:
            self.client.fput_object(self.bucket, self.object_name, local_file)
            return f"s3://{self.bucket}/{self.object_name}"
        except Exception as e:
            raise RuntimeError(f"Error uploading to S3: {e}")
        finally:
            os.remove(local_file)


class BackupDestination:
    def __init__(self, destination: str):
        self.destination = destination

    def get_strategy(self) -> BackupDestinationStrategy:
        # Use the scheme to decide the strategy.
        if str(self.destination).startswith("s3://"):
            return S3DestinationStrategy(self.destination)
        else:
            return LocalDestinationStrategy(self.destination)

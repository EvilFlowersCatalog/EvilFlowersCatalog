import logging
import os

from django.apps import AppConfig


class FilesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.files"

    def ready(self):
        # Registers the post_delete receivers that remove stored files, and IP-036's revocation receivers.
        from apps.files import signals  # noqa: F401

        if os.getenv("EVILFLOWERS_USER_ACQUISITION_MODE"):
            # Removed in IP-036: grants are reused when equivalent, whatever the mode said.
            logging.getLogger(__name__).warning(
                "EVILFLOWERS_USER_ACQUISITION_MODE is set but no longer used (IP-036): a personal request reuses "
                "the reader's live grant of the same acquisition and page range. Remove the variable."
            )

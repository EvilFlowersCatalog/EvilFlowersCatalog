from django.apps import AppConfig


class FilesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.files"

    def ready(self):
        # Registers the post_delete receivers that remove stored files.
        from apps.files import signals  # noqa: F401

from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self):
        # IP-020: keep the entry search index current.
        from apps.core.search import signals  # noqa: F401

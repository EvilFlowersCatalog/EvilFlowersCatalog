from django.apps import AppConfig


class GraphConfig(AppConfig):
    name = "apps.graph"
    label = "graph"

    def ready(self):
        from apps.graph import signals  # noqa: F401

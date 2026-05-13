from django.apps import AppConfig


class ReadiumConfig(AppConfig):
    name = "apps.readium"

    def ready(self):
        # Register lifecycle notification signal handlers (IP-003 Phase 4b).
        import apps.readium.signals  # noqa: F401

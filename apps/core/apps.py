import os

from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self):
        if os.getenv("LOGFIRE_TOKEN"):
            try:
                import logfire

                logfire.instrument_django(capture_headers=True)
            except ImportError:
                pass

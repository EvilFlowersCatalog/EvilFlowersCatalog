from django.apps import AppConfig


class ContentConfig(AppConfig):
    """Content availability (#170): content policies (#174) and the one access gate (#172)."""

    name = "apps.content"
    label = "content"

    def ready(self):
        from apps.content import settings  # noqa: F401  (registers the `content.*` instance settings)

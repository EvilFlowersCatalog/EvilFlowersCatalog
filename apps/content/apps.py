from django.apps import AppConfig


class ContentConfig(AppConfig):
    """Content availability (#170): content policies (#174), the one access gate (#172), grants and budgets
    (#175)."""

    name = "apps.content"
    label = "content"

    def ready(self):
        from apps.content import settings  # noqa: F401  (registers the `content.*` instance settings)
        from apps.content import signals  # noqa: F401  (keeps content grants in step, #175)

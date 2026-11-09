from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"

    def ready(self):
        # IP-020: keep the entry search index current.
        from apps.core.search import signals  # noqa: F401

        # #72: keep OPDS feed validators honest when an entry changes without being saved.
        from apps.core import touch  # noqa: F401

        # IP-023: keep the entry rating aggregate right when reviews are deleted by a cascade.
        from apps.core.services import reviews  # noqa: F401

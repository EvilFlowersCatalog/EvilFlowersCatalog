from django.apps import AppConfig


class ReadiumConfig(AppConfig):
    name = "apps.readium"

    def ready(self):
        # Register lifecycle notification signal handlers (IP-003 Phase 4b).
        import apps.readium.signals  # noqa: F401

        # IP-008 Phase 3 D1: register license-permission predicates on the
        # readium side. `object_checker` discovers AbacChecker subclasses
        # via `__subclasses__()`, so a side-effect import here is enough.
        import apps.readium.checkers  # noqa: F401

        # Deploy-time policy guards (readium.E001) — registered via decorator.
        import apps.readium.checks  # noqa: F401

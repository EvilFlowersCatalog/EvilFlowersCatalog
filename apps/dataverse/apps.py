from django.apps import AppConfig


class DataverseConfig(AppConfig):
    """IP-008 Phase 4: extracted from `apps/api/views/dataverse.py`.

    Owns the Dataverse <-> Catalog integration: prepublish webhook, dataset
    metadata mapping, multi-tenant catalog routing, and Celery-based
    workflow resume. IP-034: the native-API client, the Dataverse <->
    SourceRecord mapper, the pre-publish validation and the harvest
    connector `dataverse` (the sync itself is generic, `apps.harvest`).
    """

    name = "apps.dataverse"
    verbose_name = "Dataverse integration"

    def ready(self):
        from apps.dataverse import connector  # noqa: F401 — registers the `dataverse` harvest connector

from django.apps import AppConfig


class DataverseConfig(AppConfig):
    """IP-008 Phase 4: extracted from `apps/api/views/dataverse.py`.

    Owns the Dataverse <-> Catalog integration: prepublish webhook, dataset
    metadata mapping, multi-tenant catalog routing, and Celery-based
    workflow resume.
    """

    name = "apps.dataverse"
    verbose_name = "Dataverse integration"

from django.apps import AppConfig


class OaiConfig(AppConfig):
    """IP-033: the OAI-PMH 2.0 data provider at `/oai`, its record table (`oai_records`) and the sync that keeps
    it in step with the catalog; also the ISBN resolver and availability endpoints."""

    name = "apps.oai"
    label = "oai"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from apps.oai import signals  # noqa: F401

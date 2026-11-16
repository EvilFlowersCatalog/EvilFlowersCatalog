from django.apps import AppConfig


class HarvestConfig(AppConfig):
    """IP-034: the metadata harvesting client — a registry of connectors (OAI-PMH and MARC files built in),
    exact-identifier matching, link-only / enrich / create modes, `harvest.run` jobs (IP-031)."""

    name = "apps.harvest"
    label = "harvest"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Outbound two-way sync: entry changes enqueue `harvest.push` for writable `sync` sources.
        from apps.harvest import signals  # noqa: F401

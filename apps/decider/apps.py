from django.apps import AppConfig


class DeciderConfig(AppConfig):
    """The catalog's own questions to the decision model (`decider`, Proposals/Decision-Models.md): typed section
    pairs (#190) and subject suggestions with a review queue (#191)."""

    name = "apps.decider"
    label = "decider"
    default_auto_field = "django.db.models.BigAutoField"

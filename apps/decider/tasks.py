"""Night sweeps of the decision model (#190, #191), on the maintenance queue (#193)."""

from celery import shared_task
from django.conf import settings

#: Seconds past the sweep's own budget before Celery kills it (one batch may still be in flight).
_GRACE = 10 * 60


@shared_task(time_limit=settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS + _GRACE)
def edge_sweep() -> dict:
    """#190: type the similar section pairs (untyped first), before `graph-rebuild` projects them."""
    from apps.decider import client, edges

    if not client.enabled():
        return {"skipped": "EVILFLOWERS_DECIDER_URL is not set"}
    return edges.sweep()


@shared_task(time_limit=settings.EVILFLOWERS_DECIDER_SWEEP_SECONDS + _GRACE)
def subject_sweep() -> dict:
    """#191: suggest subjects for entries with at most one; file, queue for review or skip."""
    from apps.decider import client, subjects

    if not client.enabled():
        return {"skipped": "EVILFLOWERS_DECIDER_URL is not set"}
    return subjects.sweep()

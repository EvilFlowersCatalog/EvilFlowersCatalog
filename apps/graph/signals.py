"""A deleted publication leaves the graph after its relational delete commits."""

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _delete(entry_id) -> None:
    from apps.graph import projection

    try:
        projection.delete_entry(entry_id)
    except Exception:
        # The nightly rebuild drops it in any case; a graph never decides access.
        logger.exception("Could not remove entry %s from the graph", entry_id)


@receiver(post_delete, sender="core.Entry")
def entry_deleted(sender, instance, **kwargs):
    entry_id = instance.pk
    transaction.on_commit(lambda: _delete(entry_id))

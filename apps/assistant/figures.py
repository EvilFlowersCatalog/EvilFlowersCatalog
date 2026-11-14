"""Offline descriptions of figures that have no printed caption (IP-030 §3, Q5).

2,308 figures on dev have no printed caption and none has a description, so search finds them only by the
paragraph that refers to them. `describe_next` takes the next such figure of an open-access publication, asks the
model what it shows (the same request as `follow(look)`), stores the text as `Region.description` and refreshes
the region's search document. `tasks.describe_figures` runs it off-peak and yields to chats.
"""

import logging

from django.db.models import Q

logger = logging.getLogger(__name__)

#: Marked on a region's provenance when the model could not describe it, so it is not retried forever.
FAILED = "assistant_description_failed"


def candidates():
    from apps.extraction.models import Region

    return (
        Region.objects.filter(kind=Region.Kind.FIGURE, run__is_active=True)
        .exclude(image_path__isnull=True)
        .exclude(image_path="")
        .filter(Q(printed_caption__isnull=True) | Q(printed_caption=""))
        .filter(Q(description__isnull=True) | Q(description=""))
        .exclude(provenance__has_key=FAILED)
        .select_related("run__acquisition__entry")
        .order_by("-run__acquisition__entry__popularity", "page")
    )


def describe_next() -> bool:
    """Describe one figure; False when none is left (or vision is off)."""
    from apps.assistant.follow import describe, vision_enabled
    from apps.extraction import regions
    from apps.extraction.references import referring_paragraphs

    if not vision_enabled():
        return False
    region = candidates().first()
    if region is None:
        return False
    if region.run.acquisition.entry.read_config("readium_enabled"):
        # Its figures are its content: never sent to the model.
        region.provenance = {**(region.provenance or {}), FAILED: "readium"}
        region.save(update_fields=["provenance", "updated_at"])
        return True
    paragraph = referring_paragraphs([region.pk]).get(str(region.pk))
    text = describe(region, paragraph.text[:600] if paragraph else None)
    if not text:
        region.provenance = {**(region.provenance or {}), FAILED: True}
        region.save(update_fields=["provenance", "updated_at"])
        return True
    region.description = text
    region.provenance = {**(region.provenance or {}), "description_by": "elvira-assistant"}
    region.save(update_fields=["description", "provenance", "updated_at"])
    try:
        regions.index_run(region.run)
    except Exception:
        logger.exception("Re-indexing the figures of run %s failed", region.run_id)
    return True

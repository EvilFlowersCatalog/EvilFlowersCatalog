"""Background work of the assistant (IP-018 Q4)."""

import logging
from typing import Optional

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)


@shared_task
def highlight_annotation(annotation_id: str, language: Optional[str] = None) -> None:
    """Run the highlight agent for a pending agent annotation and store its highlights.

    One item per located quote, in reading order. Quotes the agent could not
    show on a page it read, and quotes the PDF search could not place, are
    counted in `unresolved_count`. Any failure leaves the annotation `failed`.
    """
    from apps.assistant import highlight, quotes
    from apps.assistant.pdf import ReaderDocument
    from apps.core.models import Annotation, AnnotationItem

    annotation = (
        Annotation.objects.select_related("user_acquisition__acquisition__entry")
        .filter(pk=annotation_id, kind=Annotation.Kind.AGENT, status=Annotation.Status.PENDING)
        .first()
    )
    if annotation is None:
        return

    try:
        entry = annotation.user_acquisition.acquisition.entry
        document = ReaderDocument.for_user_acquisition(annotation.user_acquisition)
        result = highlight.highlight(
            document,
            prompt=annotation.prompt or "",
            title=entry.title,
            authors=", ".join(author.full_name for author in entry.authors.all()),
            language=language,
        )

        items = []
        missed = 0
        with quotes.QuoteResolver.open(document) as resolver:
            for quote in result.quotes:
                located = resolver.locate(quote.text, quote.page)
                if located.match == "missed":
                    missed += 1
                    continue
                items.append(
                    AnnotationItem(
                        annotation=annotation,
                        page=located.page,
                        content=quotes.svg(located.quads, located.size),
                        match=located.match,
                        quads=located.quads,
                        text=quote.text,
                        note=quote.note or None,
                    )
                )

        # Reading order: by page, then top to bottom.
        items.sort(key=lambda item: (item.page, item.quads[0]["y0"] if item.quads else 0))
        for seq, item in enumerate(items, start=1):
            item.seq = seq

        with transaction.atomic():
            AnnotationItem.objects.bulk_create(items)
            annotation.summary = result.summary or None
            annotation.unresolved_count = missed + result.rejected
            annotation.status = Annotation.Status.DONE
            annotation.save(update_fields=["summary", "unresolved_count", "status", "updated_at"])

        logger.info(
            "Agent annotation %s: %d highlighted, %d missed, %d rejected, %d pages read, %d tokens",
            annotation.pk,
            len(items),
            missed,
            result.rejected,
            len(result.pages),
            result.tokens_used,
        )
    except Exception:
        logger.exception("Agent annotation %s failed", annotation_id)
        Annotation.objects.filter(pk=annotation_id).update(status=Annotation.Status.FAILED)

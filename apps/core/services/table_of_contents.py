"""The publication's outline (chapters) and page count, for the entry detail.

The outline is part of the PDF itself, so no extraction service is needed: it
is read here with PyMuPDF, the same way the assistant reads it, and stored on
`Entry.table_of_contents` / `Entry.page_count`. Page numbers are the reader's
(the viewer shows the license page at index 1), so a chapter opens the viewer
at the right page.
"""

import logging
from typing import List, Optional

import fitz

from apps.core.models import Acquisition, Entry

logger = logging.getLogger(__name__)

#: Headings kept per publication; a generated outline can list every paragraph.
MAX_HEADINGS = 500


def source_acquisition(entry: Entry) -> Optional[Acquisition]:
    """The entry's first stored PDF; the outline is metadata, so any relation will do."""
    return (
        entry.acquisitions.filter(
            mime=Acquisition.AcquisitionMIME.PDF,
            storage_backend=Acquisition.StorageBackend.LOCAL,
        )
        .exclude(content="")
        .exclude(content__isnull=True)
        .order_by("created_at")
        .first()
    )


def read_outline(acquisition: Acquisition) -> tuple[List[dict], int]:
    """(headings, reader page count) of a stored PDF; headings are {level, title, page}."""
    from apps.assistant.pdf import reader_source_pages

    with acquisition.content.open("rb") as file:
        document = fitz.open(stream=file.read(), filetype="pdf")
    with document:
        sources = reader_source_pages(len(document), None, acquisition.mime)
        reader_page = {source: index + 1 for index, source in enumerate(sources) if source}
        headings = [
            {"level": int(level), "title": " ".join(title.split()), "page": reader_page[page]}
            for level, title, page, *_rest in document.get_toc(simple=True)
            if page in reader_page and title and title.strip()
        ]
    return headings[:MAX_HEADINGS], len(sources)


def store_table_of_contents(entry_id) -> bool:
    """Read and store the entry's outline. False when it has no readable PDF."""
    entry = Entry.objects.filter(pk=entry_id).first()
    acquisition = entry and source_acquisition(entry)
    if acquisition is None:
        return False
    try:
        headings, page_count = read_outline(acquisition)
    except Exception:
        logger.exception("Could not read the outline of acquisition %s", acquisition.pk)
        return False
    # update(): no post_save side effects (LCP packaging, search reindex) for metadata.
    Entry.objects.filter(pk=entry.pk).update(table_of_contents=headings, page_count=page_count)
    return True

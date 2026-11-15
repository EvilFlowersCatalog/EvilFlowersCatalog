"""`GET /api/v1/locators/{entry_id}/{key}`: where a run-independent address points now (#171).

A citation, a section link or an agent's reference keeps a locator key (`apps.extraction.locators`). This view
resolves it in the entry's active run and returns the page in the reader's numbering, the box, the best sentence's
quads and, where the reader may read the text outside the viewer, the quote; also as a Readium Locator.
"""

from http import HTTPStatus
from typing import List, Optional
from uuid import UUID

from django.urls import reverse
from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.assistant.pdf import reader_source_pages
from apps.core.errors import DetailType, ProblemDetailException
from apps.core.models import Acquisition
from apps.core.views import SecuredView
from apps.extraction import locators
from apps.extraction.models import Locator
from apps.extraction.serializers import LocatorSerializer, ReadiumLocatorSerializer
from apps.openapi.types import ParameterLocation

#: Characters of the sentence a client may pass to choose the best sentence.
MAX_SENTENCE_CHARS = 1000


def reader_page(source_page: int, acquisition: Optional[Acquisition], page_count: Optional[int]) -> int:
    """The viewer's page of a page of the original file: the reader's copy carries the license page at index 1."""
    if acquisition is None:
        return source_page
    sources: List[Optional[int]] = reader_source_pages(max(page_count or 0, source_page), None, acquisition.mime)
    try:
        return sources.index(source_page) + 1
    except ValueError:
        return source_page


def _acquisition(entry_id, resolution: locators.Resolution) -> Optional[Acquisition]:
    if resolution.run is not None:
        return resolution.run.acquisition
    return (
        Acquisition.objects.filter(entry_id=entry_id, mime=Acquisition.AcquisitionMIME.PDF)
        .order_by("created_at")
        .first()
    )


def _quote(resolution: locators.Resolution) -> str:
    if resolution.paragraph is not None:
        return locators.normalise(resolution.paragraph.text)[: locators.QUOTE_CHARS]
    if resolution.region is not None and resolution.region.printed_caption:
        return locators.normalise(resolution.region.printed_caption)[: locators.QUOTE_CHARS]
    if resolution.section is not None:
        return locators.normalise(resolution.section.title)[: locators.QUOTE_CHARS]
    if getattr(resolution, "bibitem", None) is not None:
        return locators.normalise(resolution.bibitem.raw)[: locators.QUOTE_CHARS]
    return resolution.locator.quote_exact


def _quads(resolution: locators.Resolution, sentence: str) -> list:
    from apps.assistant.citations import best_sentence

    if resolution.paragraph is None:
        return []
    chosen = best_sentence(
        list(resolution.paragraph.sentences.order_by("seq")), sentence or resolution.locator.quote_exact
    )
    return list(chosen.quads or []) if chosen is not None else []


def serialize(request, entry, resolution: locators.Resolution, readable: bool, sentence: str = "") -> dict:
    locator = resolution.locator
    acquisition = _acquisition(entry.pk, resolution)
    page_count = resolution.run.page_count if resolution.run is not None else None
    page = reader_page(resolution.source_page, acquisition, page_count)
    quote = _quote(resolution) if readable else None
    href = None
    if acquisition is not None:
        href = request.build_absolute_uri(
            reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})
        )
    readium = ReadiumLocatorSerializer(
        href=href,
        type=acquisition.mime if acquisition is not None else Acquisition.AcquisitionMIME.PDF,
        locations={"position": page, "fragments": [f"page={page}"]},
        text={"before": locator.prefix, "highlight": quote, "after": locator.suffix} if readable else {},
    )
    return LocatorSerializer(
        key=locator.key,
        entry_id=entry.pk,
        kind=locator.kind,
        resolution=resolution.resolution,
        page=page,
        source_page=resolution.source_page,
        bbox=resolution.bbox,
        quads=_quads(resolution, sentence),
        readable=readable,
        quote=quote,
        detached_at=locator.detached_at,
        readium=readium,
    ).model_dump(mode="json")


class EntryLocator(SecuredView):
    @openapi.metadata(
        description=(
            "Resolve a locator: a run-independent address of a paragraph, section, figure, table or equation of "
            "a processed publication, kept by citations and agents (#171). `resolution` says how it was found: "
            "`exact` (the active extraction run has the key), `relocated` (the stored quote found within two "
            "pages: the paragraph was edited or the figure moved) or `page_only` (only the page is known). "
            "`page` is in the viewer's numbering, `source_page` in the original file's. `quote` and the Readium "
            "Locator's `text` are present only where the reader may read the text outside the viewer: never "
            "for entries lent through Readium LCP, never outside the entry's IP ranges. `?sentence=` picks the "
            "best sentence for `quads` (the one sharing most words with it)."
        ),
        tags=["Entries"],
        summary="Resolve a locator",
    )
    @openapi.parameter(
        name="sentence",
        param_type=str,
        location=ParameterLocation.QUERY,
        description="Citing sentence: chooses the paragraph's best sentence for `quads`",
        required=False,
    )
    @openapi.response(HTTPStatus.OK, "Locator", LocatorSerializer)
    def get(self, request, entry_id: UUID, key: str):
        from apps.content import access
        from apps.extraction.discovery import _entry

        entry = _entry(request, entry_id)
        resolution = locators.resolve(entry.pk, key) if len(key) <= Locator._meta.get_field("key").max_length else None
        if resolution is None:
            raise ProblemDetailException(
                _("Locator not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND
            )
        readable = access.decide(request, entry, resolution, visible=True).allows(access.Tier.SNIPPET)
        sentence = (request.GET.get("sentence") or "")[:MAX_SENTENCE_CHARS]
        return SingleResponse(request, data=serialize(request, entry, resolution, readable, sentence))

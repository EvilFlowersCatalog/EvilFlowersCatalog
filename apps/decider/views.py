"""Read endpoints for labelling the decision model's calibration cases (#201, knowledge#65).

The Restoration Studio (evilflowers-books-digitalizer#7) samples items from the catalog with GET requests only:

| Route | What |
|---|---|
| `GET /api/v1/sections/{key}/text` | a section's heading and text (its paragraphs in reading order, up to `chars`) |
| `GET /api/v1/decider/section-pairs` | every `SectionRelation` (#190) as the sweep stored it: hidden pairs included, A/B as asked |

`/sections/{key}/related` is the reader's view: cross-book only, `hide` pairs dropped and relations turned to the
reader's side, so a sample drawn through it under-represents `none`, the label calibration needs most.

Both are for the people who run the catalog: `manage` on the publication's catalog (a superuser passes). Region
labels use `locator_key` from the admin region listing (`apps.extraction.admin_views`).
"""

from datetime import datetime
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from django.db.models import Q
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission
from pydantic import ValidationInfo, model_validator

from apps import openapi
from apps.api.response import Ordering, PaginationResponse, SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import DetailType, ProblemDetailException, UnauthorizedException
from apps.core.models import UserCatalog
from apps.core.views import SecuredView
from apps.decider.models import Band, SectionRelation
from apps.extraction.models import Locator

#: Characters of section text by default, and at most.
DEFAULT_SECTION_CHARS = 4000
MAX_SECTION_CHARS = 20000


def _not_found(message: str):
    return ProblemDetailException(message, status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND)


def _int(request, name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(request.GET.get(name) or default), high))
    except ValueError:
        raise ProblemDetailException(_("Invalid %(name)s") % {"name": name}, status=HTTPStatus.BAD_REQUEST)


# -- section text ----------------------------------------------------------------------------------------------


class SectionTextSerializer(Serializer):
    key: str
    entry_id: UUID
    run_id: UUID
    #: The heading as the active run has it.
    title: str
    #: Pages of the original PDF the section spans, 1-based.
    first_page: int
    last_page: int
    #: `exact`, or `relocated` when the heading changed a little since the key was minted (`apps.extraction.locators`).
    resolution: str
    #: Paragraphs of the section, and how many of them are in `text`.
    paragraphs: int
    paragraphs_shown: int
    #: The paragraphs in reading order, whitespace collapsed, one per line, cut at `chars`.
    text: str
    truncated: bool
    #: The assistant's generated summary of the section, where there is one (unchecked, not the book's words).
    summary: Optional[str] = None


def section_text(run, title: str, chars: int) -> Tuple[str, int, int, bool]:
    """`(text, paragraphs, paragraphs shown, truncated)` of a section of `run`."""
    rows = [
        " ".join(text.split())
        for text in run.paragraphs.filter(section=title).order_by("seq").values_list("text", flat=True)
    ]
    parts, size, truncated = [], 0, False
    for text in rows:
        if size + len(text) > chars:
            # A first paragraph longer than the limit is cut, so the answer is never empty.
            if not parts:
                parts.append(text[:chars])
            truncated = True
            break
        parts.append(text)
        size += len(text) + 1
    return "\n".join(parts), len(rows), len(parts), truncated


class SectionText(SecuredView):
    @openapi.metadata(
        description=(
            "A section's heading and text by its key (the key of `/sections/{key}/related`, of the graph and of "
            "`SectionRelation`): its paragraphs in reading order, one per line, up to `chars` characters (default "
            f"{DEFAULT_SECTION_CHARS}, at most {MAX_SECTION_CHARS}), with the generated summary where there is one. "
            "For labelling decision-model cases (#201): needs `manage` on the publication's catalog. 404 for an "
            "unknown key or a section the active run no longer has."
        ),
        tags=["Decider"],
        summary="Get a section's text",
    )
    @openapi.response(HTTPStatus.OK, "Section text", SectionTextSerializer)
    def get(self, request, key: str):
        from apps.assistant.models import SectionSummary
        from apps.extraction import locators

        if not request.user.is_authenticated:
            # Before the lookup: an anonymous caller learns nothing about which keys exist.
            raise UnauthorizedException()
        locator = Locator.objects.select_related("entry__catalog").filter(key=key, kind=Locator.Kind.SECTION).first()
        if locator is None:
            raise _not_found(_("Section not found"))
        if not has_object_permission("check_entry_manage", request.user, locator.entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)
        resolution = locators.resolve(locator.entry_id, key)
        if resolution is None or resolution.section is None or resolution.run is None:
            raise _not_found(_("Section not found in the active extraction run"))

        section, run = resolution.section, resolution.run
        chars = _int(request, "chars", DEFAULT_SECTION_CHARS, 1, MAX_SECTION_CHARS)
        text, paragraphs, shown, truncated = section_text(run, section.title, chars)
        summary = SectionSummary.objects.filter(run=run, section=section.title).values_list("summary", flat=True)
        return SingleResponse(
            request,
            data=SectionTextSerializer(
                key=key,
                entry_id=locator.entry_id,
                run_id=run.pk,
                title=section.title,
                first_page=section.first_page,
                last_page=section.last_page,
                resolution=resolution.resolution,
                paragraphs=paragraphs,
                paragraphs_shown=shown,
                text=text,
                truncated=truncated,
                summary=summary.first() or None,
            ),
        )


# -- section pairs ---------------------------------------------------------------------------------------------


class SectionPairSerializer(Serializer):
    id: UUID
    #: Section A of the question, as the sweep asked it; read the relation from A to B.
    a_key: str
    b_key: str
    a_entry_id: Optional[UUID] = None
    b_entry_id: Optional[UUID] = None
    #: The headings as last seen (the locators' normalised quote).
    a_title: Optional[str] = None
    b_title: Optional[str] = None
    #: `same`, `pre` (A is a prerequisite of B), `example` (B is an example of A), `loose` or `none`.
    relation: str
    p: Optional[float] = None
    probabilities: dict = {}
    #: `keep`, `review` or `hide` (hidden from readers).
    band: str
    decider_model: str
    #: Cosine of the two section centroids (`SectionSimilarity`) in the active runs; null once the pair is no longer
    #: similar there (the relation outlives the similarity row).
    score: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def from_relation(cls, value, info: ValidationInfo):
        if not isinstance(value, SectionRelation):
            return value
        sections: Dict[str, Tuple] = (info.context or {}).get("sections", {})
        scores: Dict[frozenset, float] = (info.context or {}).get("scores", {})
        a_entry, a_title = sections.get(value.a_key, (None, None))
        b_entry, b_title = sections.get(value.b_key, (None, None))
        return {
            "id": value.pk,
            "a_key": value.a_key,
            "b_key": value.b_key,
            "a_entry_id": a_entry,
            "b_entry_id": b_entry,
            "a_title": a_title,
            "b_title": b_title,
            "relation": value.relation,
            "p": value.p,
            "probabilities": value.probabilities or {},
            "band": value.band,
            "decider_model": value.decider_model,
            "score": scores.get(frozenset((value.a_key, value.b_key))),
            "created_at": value.created_at,
            "updated_at": value.updated_at,
        }


def pair_context(relations: List[SectionRelation]) -> dict:
    """Entries, headings and similarity scores of one page of pairs."""
    from apps.extraction.models import SectionSimilarity
    from apps.graph.projection import section_key

    keys = {key for relation in relations for key in (relation.a_key, relation.b_key)}
    sections = {
        key: (entry_id, title or None)
        for key, entry_id, title in Locator.objects.filter(kind=Locator.Kind.SECTION, key__in=keys).values_list(
            "key", "entry_id", "quote_exact"
        )
    }
    entries = {entry_id for entry_id, _ in sections.values()}
    scores = {}
    if entries:
        rows = SectionSimilarity.objects.filter(
            source__is_active=True,
            target__is_active=True,
            source__acquisition__entry_id__in=entries,
            target__acquisition__entry_id__in=entries,
        ).values_list(
            "source__acquisition__entry_id",
            "source_section",
            "target__acquisition__entry_id",
            "target_section",
            "score",
        )
        for source_entry, source_section, target_entry, target_section, score in rows:
            # Keyed as the sweep keys them (`apps.decider.edges._pairs`).
            pair = frozenset((section_key(source_entry, source_section), section_key(target_entry, target_section)))
            if pair <= keys:
                scores[pair] = max(score, scores.get(pair, score))
    return {"sections": sections, "scores": scores}


class SectionPairs(SecuredView):
    @openapi.metadata(
        description=(
            "Every section pair the decision model typed (#190), as stored: `a_key`/`b_key` in the sweep's own "
            "orientation, `relation` read from A to B, and pairs hidden from readers (`band: hide`) included. With "
            "each section's publication and heading, and the pair's similarity score in the active runs. Filters: "
            "`relation`, `band`, `decider_model`, `entry_id` (either side); paged with `page`/`limit`, ordered by "
            "`order_by` (default `created_at`; also `updated_at`, `p`). For labelling calibration cases (#201): a "
            "superuser sees every pair, a catalog manager the pairs whose two sections are both in catalogs they manage."
        ),
        tags=["Decider"],
        summary="List the decider's section pairs",
    )
    @openapi.response(HTTPStatus.OK, "Section pairs", SectionPairSerializer)
    def get(self, request):
        if not request.user.is_authenticated:
            raise UnauthorizedException()
        relations = SectionRelation.objects.all()
        if not request.user.is_superuser:
            managed = UserCatalog.objects.filter(user=request.user, mode=UserCatalog.Mode.MANAGE).values("catalog_id")
            if not managed.exists():
                raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)
            own = Locator.objects.filter(kind=Locator.Kind.SECTION, entry__catalog_id__in=managed).values("key")
            relations = relations.filter(a_key__in=own, b_key__in=own)

        relation = request.GET.get("relation")
        if relation:
            if relation not in SectionRelation.Relation.values:
                raise ProblemDetailException(_("Unknown relation"), status=HTTPStatus.BAD_REQUEST)
            relations = relations.filter(relation=relation)
        band = request.GET.get("band")
        if band:
            if band not in Band.values:
                raise ProblemDetailException(_("Unknown band"), status=HTTPStatus.BAD_REQUEST)
            relations = relations.filter(band=band)
        if request.GET.get("decider_model"):
            relations = relations.filter(decider_model=request.GET["decider_model"])
        if request.GET.get("entry_id"):
            try:
                entry_id = UUID(request.GET["entry_id"])
            except ValueError:
                raise ProblemDetailException(_("Invalid identifier"), status=HTTPStatus.BAD_REQUEST)
            keys = Locator.objects.filter(kind=Locator.Kind.SECTION, entry_id=entry_id).values("key")
            relations = relations.filter(Q(a_key__in=keys) | Q(b_key__in=keys))

        ordering = Ordering.create_from_request(request)
        allowed = {"created_at", "updated_at", "p", "id"}
        if any(column.lstrip("-") not in allowed for column in ordering.columns):
            raise ProblemDetailException(_("Invalid order_by"), status=HTTPStatus.BAD_REQUEST)
        return PaginationResponse(
            request,
            relations,
            serializer=SectionPairSerializer,
            ordering=Ordering(ordering.columns + ["id"]),
            context_builder=pair_context,
        )


__all__ = ["SectionPairs", "SectionText"]

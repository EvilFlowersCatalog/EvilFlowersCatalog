from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict
from uuid import UUID

from django.db.models import Prefetch, QuerySet
from pydantic import AliasChoices, Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from apps.api.serializers import Serializer
from apps.api.serializers.feeds import FeedSerializer, feed_queryset
from apps.api.serializers.reviews import EntryRatingSerializer
from apps.core.models import Acquisition, Category, EntryAuthor


def with_entry_graph(qs: QuerySet, prefix: str = "") -> QuerySet:
    """Eager-load everything `EntrySerializer.Base` reads, so a page costs the same queries for 10 or 50 rows (#124).

    `prefix` is the path to the entry from the queryset's model, e.g. `"entry"` for licences
    or `"acquisition__entry"` for user acquisitions; `select_related` of `<prefix>__language`
    joins the entry itself as well.
    """
    p = f"{prefix}__" if prefix else ""
    # The related rows load only the columns the serializers read: every model built and every
    # timestamp parsed for nothing was CPU of each list page (k6, Nov 2026).
    return qs.select_related(f"{p}language").prefetch_related(
        # The authors joined in, one query instead of two (#135).
        Prefetch(
            f"{p}entry_authors",
            queryset=EntryAuthor.objects.select_related("author").only(
                "id", "entry_id", "author_id", "position", "author__id", "author__name", "author__surname"
            ),
        ),
        # `CategorySerializer.Base`
        Prefetch(f"{p}categories", queryset=Category.objects.only("id", "term", "label")),
        Prefetch(f"{p}feeds", queryset=feed_queryset()),
        # `AcquisitionSerializer.Base` and `Acquisition.url`
        Prefetch(
            f"{p}acquisitions",
            queryset=Acquisition.objects.only(
                "id", "entry_id", "relation", "mime", "content", "file_url", "storage_backend"
            ),
        ),
    )


#: Entry columns no list serializer reads (`EntrySerializer.Base` / `SearchResult`): the extracted text,
#: the table of contents and the search columns are kilobytes per row to transfer, decode and, with the
#: list's DISTINCT, compare. Only `EntrySerializer.Detailed` reads `content`, `identifiers` and
#: `table_of_contents`, so a list must not serialize with it.
ENTRY_LIST_DEFERRED = (
    "content",
    "identifiers",
    "table_of_contents",
    "search_vector",
    "search_authors",
    "search_subjects",
    "search_summary",
    "embedding",
)


class LcpState(str, Enum):
    NOT_LCP = "not_lcp"
    AVAILABLE_NOW = "available_now"
    AVAILABLE_IN_DAYS = "available_in_days"
    ACTIVE_LOAN_FOR_USER = "active_loan_for_user"
    FULLY_BORROWED = "fully_borrowed"


class AuthorSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        surname: str

    class Detailed(Base):
        catalog_id: UUID
        created_at: datetime
        updated_at: datetime


class CategorySerializer:
    class Base(Serializer):
        id: UUID
        term: str
        label: Dict[str, str]

    class Detailed(Base):
        catalog_id: UUID
        scheme: Optional[str]


class LanguageSerializer:
    class Base(Serializer):
        id: UUID
        name: str
        alpha2: str
        alpha3: str


class AcquisitionSerializer:
    class Nested(Serializer):
        relation: Acquisition.AcquisitionType
        mime: Acquisition.AcquisitionMIME
        url: Optional[str] = Field(validate_default=True, default=None)

        @field_validator("url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[str]:
            if not v:
                return None
            # If it's already a full URL (starts with http), return as-is
            if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                return v
            # Otherwise, build absolute URI from relative path
            return info.context["request"].build_absolute_uri(v)

    class Base(Nested):
        id: UUID

    class Detailed(Base):
        # IP-008 Phase 5: `content` (base64) and `checksum` are no longer
        # emitted by default — reading them triggered a full-file read
        # per serialization. Clients that need the bytes use the
        # `/files/acquisitions/{id}` download endpoint
        # (with `?format=base64` for the inline-base64 variant); a
        # cached checksum lives in `Acquisition.checksum_cached` and
        # can be exposed via a dedicated field if a future caller
        # needs it. Removed fields are a documented breaking change.
        pass


class EntrySerializer:
    class Base(Serializer):
        id: UUID
        creator_id: UUID
        catalog_id: UUID
        shelf_record_id: Optional[UUID] = Field(default=None, validate_default=True)
        # noinspection PyDataclass
        entry_authors: list[AuthorSerializer.Base] = Field(
            default_factory=list, validate_default=True, serialization_alias="authors"
        )
        # noinspection PyDataclass
        categories: List[CategorySerializer.Base] = Field(default_factory=list, validate_default=True)
        language: Optional[LanguageSerializer.Base]
        # noinspection PyDataclass
        feeds: List[FeedSerializer.Base] = Field(default_factory=list, validate_default=True)
        # noinspection PyDataclass
        acquisitions: List[AcquisitionSerializer.Base] = Field(default_factory=list, validate_default=True)
        popularity: int
        # IP-023: average and count of published reviews, `mine` from `serializer_context["own_ratings"]`.
        rating: Optional[EntryRatingSerializer] = Field(default=None, validate_default=True)
        title: str
        summary: Optional[str]
        image_url: Optional[str] = Field(serialization_alias="image", default=None, validate_default=True)
        image_mime: Optional[str]
        thumbnail_url: Optional[str] = Field(serialization_alias="thumbnail", default=None, validate_default=True)
        config: Optional[dict]
        citation: Optional[str]
        touched_at: datetime
        created_at: datetime
        updated_at: datetime

        # LCP availability fields. Populated via `serializer_context["lcp_states"]`
        # built once-per-request by `lcp_state_mapping(user, entries)` — same pattern as
        # `shelf_record_id` above. When the context key is absent the response advertises
        # the `not_lcp` defaults.
        lcp_state: LcpState = Field(default=LcpState.NOT_LCP, validate_default=True)
        # False while an LCP title's package is not registered yet (encryption pending,
        # failed or lost) — it then reports no free copy, whatever `total_slots` says.
        lcp_ready: bool = Field(default=False, validate_default=True)
        available_slots: int = Field(default=0, validate_default=True)
        total_slots: int = Field(default=0, validate_default=True)
        # IP-004 Phase 2: `active_count` and `over_saturated` expose legacy over-saturation
        # so operators can find and remediate entries whose stored `readium_amount`
        # was set (via direct JSON) below their current active license count.
        active_count: int = Field(default=0, validate_default=True)
        over_saturated: bool = Field(default=False, validate_default=True)
        next_available_at: Optional[datetime] = Field(default=None, validate_default=True)
        user_active_license_id: Optional[UUID] = Field(default=None, validate_default=True)
        queue_length: int = Field(default=0, validate_default=True)
        # AI extraction (IP-018), present only when the list is asked for it (`?extraction=1`,
        # an `extraction_*` sort or the `extraction_status` filter); see `annotate_entries`.
        extraction_status: Optional[str] = None
        extraction_regions: Optional[int] = None
        extraction_paragraphs: Optional[int] = None
        extraction_finished_at: Optional[datetime] = None
        user_reservation_id: Optional[UUID] = Field(default=None, validate_default=True)
        user_position: Optional[int] = Field(default=None, validate_default=True)

        @field_validator(
            "lcp_state",
            "lcp_ready",
            "available_slots",
            "total_slots",
            "active_count",
            "over_saturated",
            "next_available_at",
            "user_active_license_id",
            "queue_length",
            "user_reservation_id",
            "user_position",
            mode="before",
        )
        def _resolve_lcp_field(cls, v, info: ValidationInfo):
            if not info.context or "lcp_states" not in info.context:
                return v
            entry_id = info.data.get("id")
            row = info.context["lcp_states"].get(entry_id)
            if row is None:
                return v
            return row.get(info.field_name, v)

        @field_validator("rating", mode="before")
        def add_own_rating(cls, v, info: ValidationInfo):
            if not isinstance(v, dict):
                return v
            mine = (info.context or {}).get("own_ratings", {}).get(info.data.get("id"))
            return {**v, "mine": mine}

        @field_validator("shelf_record_id", mode="before")
        def generate_shelf_record_id(cls, v, info: ValidationInfo) -> Optional[UUID]:
            if "shelf_entries" in info.context:
                return info.context["shelf_entries"].get(info.data.get("id"), None)

        @field_validator("image_url", "thumbnail_url", mode="before")
        def generate_absolute_url(cls, v, info: ValidationInfo) -> Optional[str]:
            # `build_absolute_uri(None)` is the request's own URL; an entry without a cover has no link (#128).
            return info.context["request"].build_absolute_uri(v) if v else None

        @field_validator("categories", mode="before")
        def generate_categories(cls, v, info: ValidationInfo):
            return v.all()

        @field_validator("feeds", mode="before")
        def generate_feeds(cls, v, info: ValidationInfo):
            return v.all()

        @field_validator("entry_authors", mode="before")
        def generate_authors(cls, v, info: ValidationInfo):
            # Sort in Python so the `entry_authors__author` prefetch from the
            # list view is reused; `.order_by()` would fire a fresh query per
            # entry and turn the paginated list back into an N+1.
            return [entry_author.author for entry_author in sorted(v.all(), key=lambda ea: ea.position)]

        @field_validator("acquisitions", mode="before")
        def generate_acquisitions(cls, v, info: ValidationInfo):
            return v.all()

    class Detailed(Base):
        published_at: Optional[str]
        publisher: Optional[str]
        content: Optional[str]
        identifiers: Optional[Dict]

        # Issue #50 (partial — ratings/reviews tracked separately in #57)
        page_count: Optional[int] = None
        table_of_contents: Optional[list] = None
        related_entries: list[UUID] = Field(default_factory=list, validate_default=True)

        @field_validator("published_at", mode="before")
        def generate_published_at(cls, v, info: ValidationInfo):
            # TODO: Use Annotated
            # https://docs.pydantic.dev/latest/concepts/types/#composing-types-via-annotated
            return str(v) if v else None

        @field_validator("related_entries", mode="before")
        def generate_related_entries(cls, v, info: ValidationInfo):
            # `related_entries` is a M2M; reduce to a list of UUIDs for the serializer.
            if hasattr(v, "values_list"):
                return list(v.values_list("id", flat=True))
            return list(v) if v else []


class EntryFacetLanguage(Serializer):
    code: str = Field(validation_alias=AliasChoices("code", "value"))
    name: Optional[str] = None
    count: int


class EntryFacetCategory(Serializer):
    id: UUID = Field(validation_alias=AliasChoices("id", "value"))
    term: Optional[str] = None
    label: Dict[str, str] = Field(default_factory=dict)
    count: int


class EntryFacetFeed(Serializer):
    id: UUID = Field(validation_alias=AliasChoices("id", "value"))
    title: Dict[str, str] = Field(default_factory=dict)
    count: int


class EntryFacetAuthor(Serializer):
    id: UUID = Field(validation_alias=AliasChoices("id", "value"))
    name: str
    surname: str
    count: int


class EntryFacetValue(Serializer):
    value: str
    count: int


class EntryFacetFormat(Serializer):
    mime: str = Field(validation_alias=AliasChoices("mime", "value"))
    count: int


class EntryFacetFlag(Serializer):
    value: bool
    count: int


class EntryFacetAvailability(Serializer):
    state: str
    count: int


class EntryFacetYears(Serializer):
    min: Optional[int]
    max: Optional[int]


class EntryFacetSerializer:
    class Base(Serializer):
        """Every dimension is present unless `facet=` asked for a subset."""

        languages: Optional[List[EntryFacetLanguage]] = None
        categories: Optional[List[EntryFacetCategory]] = None
        feeds: Optional[List[EntryFacetFeed]] = None
        authors: Optional[List[EntryFacetAuthor]] = None
        publishers: Optional[List[EntryFacetValue]] = None
        formats: Optional[List[EntryFacetFormat]] = None
        fulltext: Optional[List[EntryFacetFlag]] = None
        availability: Optional[List[EntryFacetAvailability]] = None
        years: Optional[EntryFacetYears] = None


class EntrySearchHighlights(Serializer):
    title: Optional[str] = None
    summary: Optional[str] = None


class EntrySearchHit(Serializer):
    """Why an entry matched a `query`: relevance, matched fields, highlighted title/summary (HTML, `<mark>`)."""

    score: Optional[float] = None
    matched: List[str] = Field(default_factory=list)
    highlights: EntrySearchHighlights = Field(default_factory=EntrySearchHighlights)


class EntrySearchMetadata(Serializer):
    query: str
    #: `hybrid` (words + meaning, IP-021), `bm25` or `fulltext` (words only), `semantic` (meaning only)
    mode: str = "fulltext"
    #: True when embeddings are configured but the query could not be embedded: words only this time
    degraded: bool = False
    #: True when no entry contained every word and the results come from a fallback
    relaxed: bool = False
    #: `any_word` (OR of the words), `similar` (trigram similarity of title/authors) or `semantic`
    #: (only entries close in meaning), null when every word matched
    fallback: Optional[str] = None
    did_you_mean: Optional[str] = None
    total: int


class EntrySearchResult(EntrySerializer.Base):
    """An entry of `GET /api/v1/entries?query=…`: the list item plus why it matched."""

    search: Optional[EntrySearchHit] = Field(default=None, validate_default=True)

    @field_validator("search", mode="before")
    def resolve_search(cls, v, info: ValidationInfo):
        hits = (info.context or {}).get("search_hits") or {}
        return hits.get(info.data.get("id"))


EntrySerializer.SearchResult = EntrySearchResult


class SuggestedEntry(Serializer):
    id: UUID
    catalog_id: UUID
    title: str
    authors: List[str]
    thumbnail: Optional[str] = None
    #: HTML-escaped title with `<mark>` around the matched words
    highlight: Optional[str] = None


class SuggestedAuthor(Serializer):
    id: UUID
    name: str
    surname: str
    count: int


class SuggestedCategory(Serializer):
    id: UUID
    term: str
    label: Dict[str, str] = Field(default_factory=dict)
    count: int


class SuggestedFeed(Serializer):
    id: UUID
    title: Dict[str, str] = Field(default_factory=dict)
    count: int


class EntrySuggestionSerializer(Serializer):
    query: str
    entries: List[SuggestedEntry]
    authors: List[SuggestedAuthor]
    categories: List[SuggestedCategory]
    feeds: List[SuggestedFeed]
    did_you_mean: Optional[str] = None

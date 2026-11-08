import json
from http import HTTPStatus
from typing import Optional
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from object_checker.base_object_checker import has_object_permission

from apps import openapi
from apps.api.services.entry_introspection_service import EntryIntrospectionService
from apps.api.services.facets import EntryFacetService
from apps.api.services.suggest import EntrySuggestService
from apps.api.utils.parse import parse_int_query
from apps.core.errors import ValidationException, ProblemDetailException, DetailType
from apps.api.filters.entries import EntryFilter
from apps.api.forms.entries import EntryForm, AcquisitionMetaForm
from apps.api.response import (
    Ordering,
    PaginationModel,
    PaginationResponse,
    PaginationResponseModel,
    SingleResponse,
)
from apps.api.serializers.entries import (
    AcquisitionSerializer,
    EntryFacetSerializer,
    EntrySearchMetadata,
    EntrySerializer,
    EntrySuggestionSerializer,
)
from apps.api.services.entry import EntryService, attach_acquisition
from apps.core.search.query import describe_hits
from apps.core.models import Entry, Price, Catalog, ShelfRecord, User
from apps.core.views import SecuredView
from apps.readium.models import License
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping


def _assert_readium_amount_above_active(entry: Entry, form: EntryForm) -> None:
    """IP-004 Q3: reject `readium_amount` reductions below current active license count.

    The form's `populate_config` already merges incoming payload over the stored
    config, so `form.cleaned_data["config"]["readium_amount"]` is the post-merge
    value the entry would carry after save. If it is strictly less than the
    current count of READY/ACTIVE licenses, raise 409 + RFC 7807 so the operator
    must revoke (or wait for) excess licenses before reducing the cap.
    """
    config_payload = form.cleaned_data.get("config") or {}
    requested_amount = config_payload.get("readium_amount")
    if requested_amount is None:
        return

    active_count = License.objects.filter(
        entry=entry,
        state__in=[License.LicenseState.READY, License.LicenseState.ACTIVE],
        expires_at__gt=timezone.now(),
    ).count()

    if requested_amount < active_count:
        raise ProblemDetailException(
            _("Cannot reduce readium_amount below current active license count"),
            status=HTTPStatus.CONFLICT,
            detail_type=DetailType.READIUM_AMOUNT_BELOW_ACTIVE_COUNT,
            detail=_(
                "Requested readium_amount=%(requested)d is below the current "
                "active license count=%(active)d. Revoke or wait for excess "
                "licenses to terminate, then retry."
            )
            % {"requested": requested_amount, "active": active_count},
            additional_data={
                "current_active_count": active_count,
                "requested_readium_amount": requested_amount,
                "licenses_url": f"/readium/v1/licenses?entry_id={entry.pk}&state=active",
            },
        )


import logging

# Handlers/level/formatting come from Django's LOGGING config; attaching a
# StreamHandler here duplicated log lines and bypassed the project formatter.
logger = logging.getLogger("apps.api.views.entries")


def shelf_record_mapping(user: User) -> dict[UUID, UUID]:
    # An anonymous caller has no shelf. Filtering by `AnonymousUser` raises a
    # ValidationError (500) — reachable when a client's token fails to parse,
    # e.g. the portal appending `?access_token=` to a URL that already has a query.
    if not user.is_authenticated:
        return {}
    return {
        shelf_record["entry_id"]: shelf_record["id"]
        for shelf_record in ShelfRecord.objects.filter(user=user).values("entry_id", "id")
    }


class EntryListMetadata(PaginationModel):
    #: present when the request has a `query`
    search: Optional[EntrySearchMetadata] = None


class EntryListResponse(PaginationResponseModel):
    metadata: EntryListMetadata
    #: present with `include=facets`
    facets: Optional[EntryFacetSerializer.Base] = None


RELEVANCE_ORDERING = ["-relevance_score", "-popularity", "-pk"]


def entry_list_ordering(request, searching: bool) -> Ordering:
    """
    With a `query` and no `order_by` (or `order_by=relevance`), order by relevance; ties break on
    popularity and then id, so pages are stable. `relevance` without a `query` means newest first.
    """
    requested = [column.strip() for column in request.GET.get("order_by", "").split(",") if column.strip()]
    if not requested:
        return Ordering(list(RELEVANCE_ORDERING)) if searching else Ordering.create_from_request(request)

    columns = []
    for column in requested:
        if column.lstrip("-") == "relevance":
            columns.extend(RELEVANCE_ORDERING if searching else ["-created_at"])
        else:
            columns.append(column)
    return Ordering(columns)


class EntryPaginator(SecuredView):
    @openapi.metadata(
        description=(
            "Retrieve a paginated list of entries. Supports filtering by catalog, author, category, language, "
            "feed, publisher, format, publication date and availability, and full-text search (`query`). "
            "With a `query` the list is ordered by relevance unless `order_by` says otherwise "
            "(`order_by=relevance` asks for it explicitly), every item carries `search` (score, matched fields, "
            "`<mark>`-highlighted title and summary) and `metadata.search` reports whether the results come "
            "from a fallback (`relaxed`) and a `did_you_mean` suggestion. `include=facets` adds the facet "
            "counts of `GET /api/v1/entries/facets` to the response."
        ),
        tags=["Entries"],
        summary="List catalog entries with filtering and pagination",
    )
    def get(self, request):
        entry_filter = EntryFilter(request.GET, queryset=Entry.objects.all(), request=request)
        entries = (
            entry_filter.qs.select_related("language")
            .prefetch_related(
                "entry_authors__author",
                "categories",
                "feeds",
                "acquisitions",
            )
            .distinct()
        )
        search = entry_filter.search

        def context(items):
            result = {"lcp_states": lcp_state_mapping(request.user, items)}
            if search is not None:
                result["search_hits"] = describe_hits(search, items)
            return result

        include = {part.strip() for part in request.GET.get("include", "").split(",")}

        def extras(items, total):
            metadata, response = {}, {}
            if search is not None:
                metadata["search"] = EntrySearchMetadata(
                    query=search.text,
                    relaxed=search.relaxed,
                    fallback=search.fallback,
                    did_you_mean=search.did_you_mean,
                    total=total,
                )
            if "facets" in include:
                facets = EntryFacetService(request, search=search).compute()
                response["facets"] = EntryFacetSerializer.Base.model_validate(facets)
            return metadata, response

        return PaginationResponse(
            request,
            entries,
            ordering=entry_list_ordering(request, search is not None),
            serializer=EntrySerializer.SearchResult if search is not None else EntrySerializer.Base,
            serializer_context={"shelf_entries": shelf_record_mapping(request.user), "request": request},
            context_builder=context,
            response_model=EntryListResponse,
            metadata_model=EntryListMetadata,
            extra_builder=extras,
        )


class EntryFacets(SecuredView):
    @openapi.metadata(
        description=(
            "Per-value entry counts for the language, category, feed, author, publisher, format (acquisition "
            "media type), full text (`fulltext`) and availability (`lcp_state`) filters, plus the publication "
            "year range. Accepts the same query parameters as `GET /api/v1/entries`. Each dimension is computed "
            "with every active filter applied except its own, so selecting a language still shows the other "
            "languages that could be OR-ed in. Only values with at least one entry are returned, sorted by "
            "count. Authors and publishers return the top `facet_limit` values (default 10); other dimensions "
            "are complete unless `facet_limit` is given. `facet=authors,publishers` computes only the named "
            "dimensions and `facet_query` searches inside them (e.g. author names). Availability counts use "
            "the `lcp_state` values plus the `reserved` pseudo-state (an entry the user has reserved is also "
            "counted under its LCP state)."
        ),
        tags=["Entries"],
        summary="Facet counts for entry filters",
    )
    def get(self, request):
        facets = EntryFacetService(request).compute()
        return SingleResponse(request, data=EntryFacetSerializer.Base.model_validate(facets))


class EntrySuggest(SecuredView):
    @openapi.metadata(
        description=(
            "Type-ahead for the search box, in one call: the best matching entries (the last word is matched "
            "as a prefix), and the authors, categories and feeds whose name contains the text, each with the "
            "number of readable entries. Accepts `catalog_id` to stay in one catalog, `limit` (default 6, "
            "at most 20) per list. `did_you_mean` is set when no entry matches."
        ),
        tags=["Entries"],
        summary="Search suggestions (type-ahead)",
    )
    def get(self, request):
        text = (request.GET.get("query") or "").strip()
        limit = parse_int_query(request, "limit", default=6, min_value=1)
        result = EntrySuggestService(request, text, limit=min(limit, 20), catalog_id=request.GET.get("catalog_id"))
        return SingleResponse(request, data=EntrySuggestionSerializer.model_validate(result.compute()))


class EntryIntrospection(SecuredView):
    @openapi.metadata(
        description="Fetch metadata for a publication from external sources using various identifiers (ISBN, DOI, etc.). This endpoint allows you to retrieve detailed information about a book or publication before creating an entry in the catalog.",
        tags=["Entries"],
        summary="Fetch entry metadata from external sources",
    )
    def get(self, request):
        service = EntryIntrospectionService(request.GET.get("driver"))
        result = service.resolve(request.GET.get("identifier"))

        return SingleResponse(request, data=result)


class EntryManagement(SecuredView):
    @openapi.metadata(
        description="Create a new entry in the specified catalog. An entry represents a publication (book, article, etc.) with metadata like title, authors, publication date, and associated acquisition files.",
        tags=["Entries"],
        summary="Create a new catalog entry",
    )
    @method_decorator(transaction.atomic)
    def post(self, request, catalog_id: UUID):
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except Catalog.DoesNotExist as e:
            raise ProblemDetailException(_("Catalog not found"), status=HTTPStatus.NOT_FOUND, previous=e)

        if not has_object_permission("check_catalog_write", request.user, catalog):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        form = EntryForm.create_from_request(request)
        form.fields["category_ids"].queryset = form.fields["category_ids"].queryset.filter(catalog=catalog)
        form.fields["author_ids"].queryset = form.fields["author_ids"].queryset.filter(catalog=catalog)

        if not form.is_valid():
            raise ValidationException(form)

        entry = Entry(creator=request.user, catalog=catalog)
        service = EntryService(catalog, request.user)

        try:
            service.populate(entry, form)
        except EntryService.AlreadyExists as e:
            raise ProblemDetailException(
                "Entry already exists!",
                status=HTTPStatus.CONFLICT,
                detail=_("Entry with same title, isbn or DOI already exists in catalog %s") % (catalog.title,),
                previous=e,
                detail_type=DetailType.CONFLICT,
            )

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry,
                context={
                    "shelf_entries": shelf_record_mapping(request.user),
                    "request": request,
                    "lcp_states": lcp_state_mapping(request.user, [entry]),
                },
            ),
            status=HTTPStatus.CREATED,
        )


class EntryDetail(SecuredView):
    @staticmethod
    def get_entry(
        request,
        catalog_id: UUID,
        entry_id: UUID,
        checker: str = "check_entry_manage",
    ) -> Entry:
        try:
            entry = (
                Entry.objects.select_related("language")
                .prefetch_related(
                    "entry_authors__author",
                    "categories",
                    "feeds",
                    "acquisitions",
                )
                .get(pk=entry_id, catalog_id=catalog_id)
            )
        except Entry.DoesNotExist:
            raise ProblemDetailException(_("Entry not found"), status=HTTPStatus.NOT_FOUND)

        if not has_object_permission(checker, request.user, entry):
            raise ProblemDetailException(_("Insufficient permissions"), status=HTTPStatus.FORBIDDEN)

        return entry

    @openapi.metadata(
        description="Retrieve detailed information about a specific entry including its metadata, authors, categories, acquisition files, and user-specific information like shelf status.",
        tags=["Entries"],
        summary="Get entry details",
    )
    def get(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id, "check_entry_read")

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry,
                context={
                    "shelf_entries": shelf_record_mapping(request.user),
                    "request": request,
                    "lcp_states": lcp_state_mapping(request.user, [entry]),
                },
            ),
        )

    @openapi.metadata(
        description="Create a new acquisition file for the specified entry. Acquisitions represent downloadable content (PDF, EPUB, etc.) associated with an entry, including file metadata and optional pricing information.",
        tags=["Entries"],
        summary="Create acquisition file for entry",
    )
    def post(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)

        try:
            metadata = json.loads(request.POST.get("metadata", "{}"))
        except json.JSONDecodeError as e:
            raise ProblemDetailException(
                title=_("Unable to parse metadata for request file"),
                status=HTTPStatus.BAD_REQUEST,
                previous=e,
            )

        form = AcquisitionMetaForm(metadata, request)

        if not form.is_valid():
            raise ValidationException(form)

        if "content" not in request.FILES:
            raise ProblemDetailException(_("Missing `content` file"), status=HTTPStatus.BAD_REQUEST)

        acquisition = attach_acquisition(
            entry,
            request.FILES["content"],
            mime=request.FILES["content"].content_type,
            relation=form.cleaned_data.get("relation"),
        )

        for price in form.cleaned_data.get("prices", []):
            Price.objects.create(
                acquisition=acquisition,
                currency=price["currency_code"],
                value=price["value"],
            )

        return SingleResponse(
            request,
            data=AcquisitionSerializer.Detailed.model_validate(acquisition, context={"request": request}),
            status=HTTPStatus.CREATED,
        )

    @openapi.metadata(
        description="Update the metadata of an existing entry. This includes title, summary, authors, categories, publication information, and configuration settings for features like OCR and annotations.",
        tags=["Entries"],
        summary="Update entry metadata",
    )
    def put(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)

        form = EntryForm.create_from_request(request)
        form.fields["category_ids"].queryset = form.fields["category_ids"].queryset.filter(catalog_id=catalog_id)
        form.fields["author_ids"].queryset = form.fields["author_ids"].queryset.filter(catalog_id=catalog_id)

        if not form.is_valid():
            raise ValidationException(form)

        # IP-004 Q3: hard-reject cap reductions that would over-saturate.
        _assert_readium_amount_above_active(entry, form)

        catalog = Catalog.objects.get(pk=catalog_id)
        service = EntryService(catalog, request.user)

        try:
            service.populate(entry, form)
        except EntryService.AlreadyExists as e:
            raise ProblemDetailException(
                "Entry already exists!",
                status=HTTPStatus.CONFLICT,
                previous=e,
                detail=_("Entry with same title, isbn or DOI already exists in catalog %s") % (catalog.title,),
            )

        return SingleResponse(
            request,
            data=EntrySerializer.Detailed.model_validate(
                entry,
                context={
                    "shelf_entries": shelf_record_mapping(request.user),
                    "request": request,
                    "lcp_states": lcp_state_mapping(request.user, [entry]),
                },
            ),
        )

    @openapi.metadata(
        description=(
            "Remove an entry from the catalog together with its user data (annotations, shelf records, loan "
            "history). Its stored files — acquisitions, cover, thumbnail, extraction crops and the Readium LCP "
            "package — are removed from storage once the deletion commits. The LCP server keeps its own record "
            "of an encrypted publication. This action is irreversible."
        ),
        tags=["Entries"],
        summary="Delete catalog entry",
    )
    def delete(self, request, catalog_id: UUID, entry_id: UUID):
        entry = self.get_entry(request, catalog_id, entry_id)
        entry.delete()

        return SingleResponse(request)

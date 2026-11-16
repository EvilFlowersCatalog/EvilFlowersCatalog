"""IP-033 endpoints.

| Route | |
|---|---|
| `GET, POST /oai` | OAI-PMH 2.0 data provider; anonymous, CSRF-exempt, `text/xml`, errors as OAI XML with HTTP 200 |
| `GET /resolve/isbn/{isbn}` | 302 to the portal entry of an exposed publication with that ISBN (Q10) |
| `GET /availability?isbn=` | copies and loan state of exposed publications with that ISBN (Q10) |
| `GET /api/v1/oai/status` | record counts and datestamps, for superusers and catalog managers |

`/oai` is a plain view, so nothing in front of it answers with a JSON problem document. The resolver and
availability are `SecuredView`s without authentication: they count against the rate limits (IP-026).
Both read only live `oai_records`, so they answer for exposed entries and nothing else.
"""

from http import HTTPStatus
from typing import Optional

from django.conf import settings
from django.db.models import Count, Max, Min, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps import openapi
from apps.api.response import SingleResponse
from apps.api.serializers import Serializer
from apps.core.errors import AuthorizationException, DetailType, ProblemDetailException
from apps.core.models import UserCatalog
from apps.core.views import SecuredView
from apps.harvest.records import normalize_isbn
from apps.oai.models import OaiRecord
from apps.oai.protocol import format_datestamp
from apps.oai.repository import OaiRepository

CONTENT_TYPE = "text/xml; charset=utf-8"


def _enabled():
    if not settings.EVILFLOWERS_OAI_ENABLED:
        raise Http404()


@method_decorator(csrf_exempt, name="dispatch")
class OaiPmhView(View):
    """Anonymous by design: harvesters send no credentials, and the records are public metadata."""

    http_method_names = ["get", "post"]

    @openapi.metadata(
        description=(
            "OAI-PMH 2.0 data provider (IP-033): `verb` = Identify, ListMetadataFormats, ListSets, GetRecord, "
            "ListIdentifiers or ListRecords with the protocol's arguments. Formats `oai_dc` and `marc21`; sets are "
            "harvestable catalogs (`url_name`) and their categories (`url_name:category id`). Always "
            "`text/xml; charset=utf-8`; protocol errors are OAI `<error>` elements with HTTP 200. Anonymous."
        ),
        tags=["OAI-PMH"],
        summary="OAI-PMH request",
    )
    @openapi.response(HTTPStatus.NOT_FOUND, "OAI-PMH is turned off on this instance (`EVILFLOWERS_OAI_ENABLED`)")
    def get(self, request):
        return self._respond(request.GET)

    @openapi.metadata(
        description="The same OAI-PMH request with its arguments in an `application/x-www-form-urlencoded` body.",
        tags=["OAI-PMH"],
        summary="OAI-PMH request (form POST)",
    )
    @openapi.response(HTTPStatus.NOT_FOUND, "OAI-PMH is turned off on this instance (`EVILFLOWERS_OAI_ENABLED`)")
    def post(self, request):
        # §3.1.1.2: arguments in an application/x-www-form-urlencoded body.
        return self._respond(request.POST)

    @staticmethod
    def _respond(params) -> HttpResponse:
        _enabled()
        return HttpResponse(OaiRepository().respond(params), content_type=CONTENT_TYPE)


def _not_found() -> ProblemDetailException:
    return ProblemDetailException(_("Not found"), status=HTTPStatus.NOT_FOUND, detail_type=DetailType.NOT_FOUND)


def _live_records(isbn: Optional[str]):
    """Live records with this ISBN, as the projection normalised it (ISBN-13)."""
    normalized = normalize_isbn(isbn)
    if not normalized:
        return OaiRecord.objects.none(), None
    records = OaiRecord.objects.filter(deleted=False, projection__identifiers__isbn=normalized)
    return records.order_by("datestamp", "id"), normalized


class IsbnResolveView(SecuredView):
    authenticates = False

    @openapi.metadata(
        description=(
            "Redirects (302) to the portal entry of the harvestable publication with this ISBN (ISBN-10 or -13, "
            "hyphens allowed). 404 for an unknown or invalid ISBN, a publication outside harvestable catalogs, or "
            "while `content.cite_base_url` is empty (no portal URL). Anonymous (IP-033 Q10)."
        ),
        tags=["OAI-PMH"],
        summary="Resolve an ISBN to the portal",
    )
    @openapi.response(HTTPStatus.NOT_FOUND, "Unknown or invalid ISBN, not harvestable, or no portal URL configured")
    def get(self, request, isbn: str):
        _enabled()
        records, _normalized = _live_records(isbn)
        for record in records:
            url = (record.projection or {}).get("url")
            if url:
                return HttpResponseRedirect(url)
        raise _not_found()


class AvailabilityView(SecuredView):
    authenticates = False

    @openapi.metadata(
        description=(
            "Loan state of the harvestable publications with the ISBN in `?isbn=`: `{isbn, entries: [{id, title, "
            "lcp, copies, available, next_free}]}`. For a Readium LCP title `copies` is the number of licences, "
            "`available` how many can be borrowed now and `next_free` when the earliest loan ends; for an open "
            "title all three are null (no loan limit). `isbn` is the normalised ISBN-13. Anonymous (IP-033 Q10)."
        ),
        tags=["OAI-PMH"],
        summary="Availability by ISBN",
    )
    def get(self, request):
        _enabled()
        from apps.core.models import Entry
        from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

        records, normalized = _live_records(request.GET.get("isbn"))
        if not normalized:
            raise ProblemDetailException(
                _("A valid ISBN is required"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )
        entry_ids = [record.entry_id for record in records]
        entries = list(Entry.objects.filter(pk__in=entry_ids).only("id", "title", "config"))
        states = lcp_state_mapping(None, entries)
        order = {entry_id: index for index, entry_id in enumerate(entry_ids)}
        payload = []
        for entry in sorted(entries, key=lambda item: order[item.pk]):
            lcp = bool(entry.read_config("readium_enabled"))
            state = states.get(entry.pk) or {}
            next_free = state.get("next_available_at") if lcp else None
            payload.append(
                {
                    "id": str(entry.pk),
                    "title": entry.title,
                    "lcp": lcp,
                    "copies": state.get("total_slots") if lcp else None,
                    "available": state.get("available_slots") if lcp else None,
                    "next_free": next_free.isoformat() if next_free else None,
                }
            )
        return JsonResponse({"isbn": normalized, "entries": payload})


class OaiStatusSerializer(Serializer):
    enabled: bool
    base_url: str
    repository_identifier: str
    #: Records a harvester gets in full.
    live: int
    #: Tombstones (`status="deleted"` headers), kept forever.
    deleted: int
    earliest_datestamp: Optional[str] = None
    last_datestamp: Optional[str] = None


class OaiStatusView(SecuredView):
    @openapi.metadata(
        description=(
            "The OAI-PMH data provider at a glance: base URL, repository identifier, live and deleted record "
            "counts, earliest and last datestamp. Superusers and managers of any catalog."
        ),
        tags=["OAI-PMH"],
        summary="Get OAI-PMH status",
    )
    @openapi.response(HTTPStatus.OK, "Status", OaiStatusSerializer)
    def get(self, request):
        user = request.user
        if not user.is_authenticated or not (
            user.is_superuser or UserCatalog.objects.filter(user=user, mode=UserCatalog.Mode.MANAGE).exists()
        ):
            raise AuthorizationException(request)
        counts = OaiRecord.objects.aggregate(
            live=Count("id", filter=Q(deleted=False)),
            deleted=Count("id", filter=Q(deleted=True)),
            earliest=Min("datestamp"),
            latest=Max("datestamp"),
        )
        payload = OaiStatusSerializer(
            enabled=settings.EVILFLOWERS_OAI_ENABLED,
            base_url=OaiRepository().base_url,
            repository_identifier=settings.EVILFLOWERS_OAI_REPOSITORY_IDENTIFIER,
            live=counts["live"],
            deleted=counts["deleted"],
            earliest_datestamp=format_datestamp(counts["earliest"]) if counts["earliest"] else None,
            last_datestamp=format_datestamp(counts["latest"]) if counts["latest"] else None,
        )
        return SingleResponse(request, data=payload)


__all__ = ["AvailabilityView", "IsbnResolveView", "OaiPmhView", "OaiStatusView"]

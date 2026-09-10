"""The caller's own library: identity, shelf, and Readium LCP loans.

Every tool here is scoped to `request.user` by construction — none of them take
a user argument, so there is no path by which a model can ask about somebody
else's borrowing history.
"""

from django.conf import settings
from django.utils.translation import gettext as _

from apps.core.models import Catalog, ShelfRecord, UserCatalog
from apps.mcp.arguments import Arguments
from apps.mcp.pagination import PAGINATION_ARGUMENTS, pagination_schema, paginate, read_pagination
from apps.mcp.projections import license_summary, shelf_record
from apps.mcp.registry import ToolAccess, registry
from apps.mcp.schemas import ENTRY_SUMMARY_SCHEMA, LICENSE_SCHEMA, UUID_SCHEMA, list_output
from apps.readium.models import License
from apps.readium.services.entry_lcp_decorator import lcp_state_mapping

LICENSE_STATES = tuple(state.value for state in License.LicenseState)


@registry.tool(
    name="whoami",
    title="Identify the current session",
    description=(
        "Report who the current credential authenticates as and how much of the catalog it can "
        "reach. Call this first when a request depends on the user's own data ('my loans', "
        "'books I saved') so you know whether the session is authenticated at all."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema={
        "type": "object",
        "properties": {
            "authenticated": {"type": "boolean"},
            "user_id": UUID_SCHEMA,
            "username": {"type": "string"},
            "full_name": {"type": "string"},
            "is_superuser": {"type": "boolean"},
            "readable_catalogs": {"type": "integer"},
            "manageable_catalogs": {"type": "integer"},
            "can_write": {"type": "boolean"},
            "access": {"type": "string"},
        },
        "required": ["authenticated"],
    },
)
def whoami(request, raw_arguments: dict) -> dict:
    Arguments(raw_arguments, allowed=())
    user = request.user

    if not user.is_authenticated:
        return {
            "authenticated": False,
            "can_write": False,
            "access": _(
                "Anonymous session: public catalogs only. Personal tools (shelf, loans) and all "
                "management tools are unavailable. Supply an `Authorization: Bearer <api key>` header."
            ),
        }

    if user.is_superuser:
        manageable = Catalog.objects.count()
    else:
        manageable = UserCatalog.objects.filter(user=user, mode=UserCatalog.Mode.MANAGE).count()

    return {
        "authenticated": True,
        "user_id": str(user.pk),
        "username": user.username,
        "full_name": user.full_name,
        "is_superuser": user.is_superuser,
        "readable_catalogs": user.catalogs.count(),
        # A model should know before it tries. `manageable_catalogs == 0` means
        # every create/update/delete tool will refuse, whatever the write
        # surface is set to.
        "manageable_catalogs": manageable,
        "can_write": settings.EVILFLOWERS_MCP_ALLOW_WRITE and manageable > 0,
    }


@registry.tool(
    name="get_my_shelf",
    title="List my saved publications",
    description=(
        "List the publications the authenticated user has saved to their shelf, newest first. "
        "The shelf is a bookmark list — it is not the same as an active loan; use "
        "`list_my_loans` for borrowed titles."
    ),
    input_schema={"type": "object", "properties": {**pagination_schema()}, "additionalProperties": False},
    output_schema=list_output(
        {
            "type": "object",
            "properties": {
                "id": UUID_SCHEMA,
                "added_at": {"type": "string", "format": "date-time"},
                "entry": ENTRY_SUMMARY_SCHEMA,
            },
            "required": ["id", "entry"],
        }
    ),
    access=ToolAccess.USER,
)
def get_my_shelf(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=PAGINATION_ARGUMENTS)
    requested_page = read_pagination(arguments)

    records = (
        ShelfRecord.objects.filter(user=request.user)
        .select_related("entry", "entry__language")
        .prefetch_related("entry__entry_authors__author", "entry__categories")
        .order_by("-created_at")
    )
    page = paginate(records, requested_page)
    lcp_states = lcp_state_mapping(request.user, [record.entry for record in page.items])

    return {
        "items": [shelf_record(record, request, lcp_states) for record in page.items],
        "metadata": page.metadata(),
    }


@registry.tool(
    name="list_my_loans",
    title="List my Readium LCP loans",
    description=(
        "List the authenticated user's DRM (Readium LCP) licences — books currently borrowed "
        "plus, optionally, past ones. Each entry carries the loan window and how many times it "
        "has been renewed. Defaults to live loans ('ready' and 'active') only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "states": {
                "type": "array",
                "items": {"type": "string", "enum": list(LICENSE_STATES)},
                "description": (
                    "Licence states to include. 'ready' = issued, not yet opened; 'active' = in "
                    "use; 'returned' / 'expired' / 'revoked' / 'cancelled' are finished loans. "
                    "Defaults to ['ready', 'active']."
                ),
            },
            **pagination_schema(),
        },
        "additionalProperties": False,
    },
    output_schema=list_output(LICENSE_SCHEMA),
    access=ToolAccess.USER,
)
def list_my_loans(request, raw_arguments: dict) -> dict:
    arguments = Arguments(raw_arguments, allowed=("states", *PAGINATION_ARGUMENTS))
    requested_page = read_pagination(arguments)

    states = arguments.enum_list("states", LICENSE_STATES)
    states = states.split(",") if states else [License.LicenseState.READY, License.LicenseState.ACTIVE]

    licenses = (
        License.objects.filter(user=request.user, state__in=states).select_related("entry").order_by("-created_at")
    )
    page = paginate(licenses, requested_page)

    return {
        "items": [license_summary(item, request) for item in page.items],
        "metadata": page.metadata(),
    }

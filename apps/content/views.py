"""Content policies over REST (#174): read, create, edit (new version), assign, explain."""

import json
from http import HTTPStatus

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.content import access, policies, services
from apps.core.errors import ProblemDetailException
from apps.core.views import SecuredView


def _problem(error: Exception) -> ProblemDetailException:
    if isinstance(error, services.Refused):
        return ProblemDetailException(error.message, status=HTTPStatus(error.status), previous=error)
    return ProblemDetailException(
        error.message,
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        additional_data={"path": error.path, "schema_path": error.schema_path},
        previous=error,
    )


def _body(request) -> dict:
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError as error:
        raise ProblemDetailException(_("The body must be JSON."), status=HTTPStatus.BAD_REQUEST, previous=error)
    if not isinstance(payload, dict):
        raise ProblemDetailException(_("The body must be a JSON object."), status=HTTPStatus.BAD_REQUEST)
    return payload


def _dry_run(request) -> bool:
    return request.GET.get("dry_run", "").lower() in ("1", "true", "yes")


def _policy(key: str) -> policies.Policy:
    policy = policies.current(key)
    if policy is None:
        raise ProblemDetailException(_("Content policy not found"), status=HTTPStatus.NOT_FOUND)
    return policy


class ContentPolicyCollection(SecuredView):
    @openapi.metadata(
        description=(
            "Every content policy at its current version: key, version, title and the document (tiers per "
            "basis, caps, grants, budget, log). Catalogs and entries refer to one by key."
        ),
        tags=["Content policies"],
        summary="List content policies",
    )
    def get(self, request):
        return SingleResponse(request, data=[services.policy_payload(policy) for policy in policies.all_current()])

    @openapi.metadata(
        description=(
            'Create a content policy from a document (`{"document": {...}}`, validated against '
            "`/content-policies/schema`; 422 names the `path` and the `schema_path` of the error). Takes effect "
            "as version 1. `?dry_run=true` validates only. Administrators only."
        ),
        tags=["Content policies"],
        summary="Create a content policy",
    )
    def post(self, request):
        try:
            services.require_superuser(request.user)
            document = _body(request).get("document")
            key = document.get("key") if isinstance(document, dict) else None
            if not key:
                raise policies.PolicyError("The document needs a `key`.", path="key")
            policy, _changed = policies.save(key, document, user=request.user, dry_run=_dry_run(request), create=True)
        except (services.Refused, policies.PolicyError) as error:
            raise _problem(error)
        return SingleResponse(request, data=services.policy_payload(policy), status=HTTPStatus.CREATED)


class ContentPolicySchema(SecuredView):
    @openapi.metadata(
        description="The JSON Schema every policy document must match; the portal renders its form from it.",
        tags=["Content policies"],
        summary="Content policy schema",
    )
    def get(self, request):
        return SingleResponse(request, data=policies.SCHEMA)


class ContentPolicyExplain(SecuredView):
    @openapi.metadata(
        description=(
            "Why the gate decides what it does for one entry: `{entry_id, user_id?, surface?, processor?, policy?}` "
            "→ basis, policy `key@version` and where it came from, each cap applied and the final tier. `user_id` "
            "(administrators) explains for another reader with your IP address; `policy` shows what another "
            "policy would decide without assigning it."
        ),
        tags=["Content policies"],
        summary="Explain content access",
    )
    def post(self, request):
        payload = _body(request)
        processor = payload.get("processor")
        if processor not in (None, "local", "external"):
            raise ProblemDetailException(_("`processor` is `local` or `external`."), status=HTTPStatus.BAD_REQUEST)
        if not payload.get("entry_id"):
            raise ProblemDetailException(_("`entry_id` is required."), status=HTTPStatus.BAD_REQUEST)
        try:
            data = services.explain(
                request,
                payload["entry_id"],
                user_id=payload.get("user_id"),
                surface=payload.get("surface") or access.Surface.REST,
                processor=processor,
                policy_key=payload.get("policy"),
            )
        except (services.Refused, policies.PolicyError) as error:
            raise _problem(error)
        return SingleResponse(request, data=data)


class CiteReferences(SecuredView):
    @openapi.metadata(
        description=(
            "Formatted references for citation addresses (#186), the MCP `cite` tool's payload: `{uris: [...], style}` "
            "→ `{style, references: [{uri, text | csl}]}`. An address is `evilflowers://entry/<id>/locator/<key>` (a "
            "chat citation's `uri`), a `/cite/<id>/<key>` link, absolute or relative (its `url`), or "
            "`evilflowers://entry/<id>` for the whole publication. `style` is `iso690` (default, as Slovak theses "
            "use), `apa`, `bibtex` or `csl-json`. At most 50 addresses; each reference carries the page and the "
            "`/cite` link. Same access rules as the MCP tool: 404 for a publication or locator the caller may not "
            "see."
        ),
        tags=["Content"],
        summary="Format references",
    )
    def post(self, request):
        from apps.content import reading

        payload = _body(request)
        addresses = payload.get("uris")
        if not isinstance(addresses, list) or not addresses or not all(isinstance(item, str) for item in addresses):
            raise ProblemDetailException(
                _("`uris` must be a non-empty array of addresses."), status=HTTPStatus.BAD_REQUEST
            )
        if len(addresses) > reading.MAX_REFERENCES:
            raise ProblemDetailException(
                _("At most %(count)s addresses per request.") % {"count": reading.MAX_REFERENCES},
                status=HTTPStatus.BAD_REQUEST,
            )
        try:
            data = reading.references(request, addresses, str(payload.get("style") or "iso690"))
        except reading.NotFound as error:
            raise ProblemDetailException(str(error), status=HTTPStatus.NOT_FOUND, previous=error)
        except reading.Invalid as error:
            raise ProblemDetailException(str(error), status=HTTPStatus.BAD_REQUEST, previous=error)
        return SingleResponse(request, data=data)


class ContentPolicyDetail(SecuredView):
    @openapi.metadata(
        description="One content policy at its current version.",
        tags=["Content policies"],
        summary="Get a content policy",
    )
    def get(self, request, key: str):
        return SingleResponse(request, data=services.policy_payload(_policy(key)))

    @openapi.metadata(
        description=(
            'Save a new version of a content policy (`{"document": {...}}`). It takes effect immediately; older '
            "versions stay readable under `/versions`. An identical document writes nothing. `?dry_run=true` "
            "validates only. Administrators only."
        ),
        tags=["Content policies"],
        summary="Update a content policy",
    )
    def put(self, request, key: str):
        try:
            services.require_superuser(request.user)
            policy, changed = policies.save(
                key, _body(request).get("document"), user=request.user, dry_run=_dry_run(request), create=False
            )
        except (services.Refused, policies.PolicyError) as error:
            raise _problem(error)
        return SingleResponse(request, data={**services.policy_payload(policy), "changed": changed})


class ContentPolicyVersions(SecuredView):
    @openapi.metadata(
        description="Every stored version of a content policy, newest first; `?version=N` returns that document.",
        tags=["Content policies"],
        summary="List content policy versions",
    )
    def get(self, request, key: str):
        _policy(key)
        number = request.GET.get("version")
        if number:
            try:
                policy = policies.version(key, int(number))
            except ValueError:
                policy = None
            if policy is None:
                raise ProblemDetailException(_("Content policy version not found"), status=HTTPStatus.NOT_FOUND)
            return SingleResponse(request, data=services.policy_payload(policy))
        return SingleResponse(request, data=policies.versions(key))


class ContentPolicyAssign(SecuredView):
    @openapi.metadata(
        description=(
            'Assign this policy to a catalog and/or entries: `{"catalog_id"?, "entry_ids"?: [...]}`. Needs '
            "`manage` on each catalog involved. The key `inherit` clears the assignment instead. "
            "`?dry_run=true` reports what would change."
        ),
        tags=["Content policies"],
        summary="Assign a content policy",
    )
    def post(self, request, key: str):
        payload = _body(request)
        entry_ids = payload.get("entry_ids") or []
        if not isinstance(entry_ids, list):
            raise ProblemDetailException(_("`entry_ids` must be a list."), status=HTTPStatus.BAD_REQUEST)
        try:
            result = services.assign(
                request,
                None if key == "inherit" else key,
                catalog_id=payload.get("catalog_id"),
                entry_ids=entry_ids,
                dry_run=_dry_run(request),
            )
        except (services.Refused, policies.PolicyError) as error:
            raise _problem(error)
        return SingleResponse(request, data=result.payload())

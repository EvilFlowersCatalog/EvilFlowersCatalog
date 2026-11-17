"""Instance settings over REST (#20): the registry with current values, and writes. Superusers only, except
the listing filtered to `public=true` (#212), which anyone reads."""

import json
from http import HTTPStatus
from typing import Dict, Optional

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import GeneralResponse, SingleResponse, SingleResponseModel
from apps.core import capabilities, settings_store
from apps.core.errors import ProblemDetailException
from apps.core.views import SecuredView


def _require_superuser(request):
    if not request.user.is_superuser:
        raise ProblemDetailException(
            _("Instance settings are restricted to administrators."), status=HTTPStatus.FORBIDDEN
        )


def _definition(key: str):
    try:
        return settings_store.definition(key)
    except settings_store.SettingError as error:
        raise ProblemDetailException(error.message, status=HTTPStatus.NOT_FOUND, previous=error)


def _json_body(request) -> dict:
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError as error:
        raise ProblemDetailException(_("The body must be JSON."), status=HTTPStatus.BAD_REQUEST, previous=error)
    if not isinstance(payload, dict) or "value" not in payload:
        raise ProblemDetailException(
            _('The body must be a JSON object with "value" (null resets the key).'), status=HTTPStatus.BAD_REQUEST
        )
    return payload


def _refused(error: settings_store.SettingError) -> ProblemDetailException:
    return ProblemDetailException(
        error.message,
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        additional_data={"path": error.path, "schema_path": error.schema_path},
        previous=error,
    )


class SettingListResponse(SingleResponseModel):
    #: present with `include=capabilities`
    capabilities: Optional[Dict[str, bool]] = None


def _public_filter(request) -> Optional[bool]:
    raw = request.GET.get("public")
    if raw is None or raw == "":
        return None
    value = raw.lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    raise ProblemDetailException(_("`public` must be true or false."), status=HTTPStatus.BAD_REQUEST)


class SettingCollection(SecuredView):
    @openapi.metadata(
        description=(
            "Every instance setting the code declares: its key, JSON Schema, description, default, the Django "
            "setting it falls back to, `public`, the effective `value` and its `source` (`stored`, `setting` or "
            "`default`). The portal renders its settings form from this. Administrators only, except "
            "`public=true`: the keys marked public (`portal.features`, ...), which anyone may read, anonymous "
            "readers included. A secret key is never public. `include=capabilities` adds `capabilities`, what "
            "the deployment can do whatever the settings say (`assistant`, `extraction`, `hybrid_search`, "
            "`lcp`, `graph`, `harvest`); the portal hides a feature whose capability is false (#212)."
        ),
        tags=["Settings"],
        summary="List instance settings",
    )
    def get(self, request):
        public = _public_filter(request)
        if public is not True:
            _require_superuser(request)
        include = {part.strip() for part in request.GET.get("include", "").split(",") if part.strip()}
        payload = SettingListResponse(
            response=settings_store.describe_all(public=public),
            capabilities=capabilities.states() if "capabilities" in include else None,
        )
        return GeneralResponse(request, data=payload)


class SettingDetail(SecuredView):
    @openapi.metadata(
        description="One instance setting with its schema, effective value and source. Administrators only.",
        tags=["Settings"],
        summary="Get an instance setting",
    )
    def get(self, request, key: str):
        _require_superuser(request)
        _definition(key)
        return SingleResponse(request, data=settings_store.describe(key))

    @openapi.metadata(
        description=(
            'Store a setting: `{"value": ...}`, validated against the key\'s schema (422 with the schema `path` '
            'of the error). `{"value": null}` deletes the stored value so the key inherits again. `?dry_run=true` '
            "validates without writing. Applies to the next request. Administrators only."
        ),
        tags=["Settings"],
        summary="Update an instance setting",
    )
    def put(self, request, key: str):
        _require_superuser(request)
        _definition(key)
        payload = _json_body(request)
        dry_run = request.GET.get("dry_run", "").lower() in ("1", "true", "yes")
        try:
            result = settings_store.update(key, payload["value"], user=request.user, dry_run=dry_run)
        except settings_store.SettingError as error:
            raise _refused(error)
        return SingleResponse(request, data={**settings_store.describe(key), **result})

"""
Dataverse workflow webhooks.

Thin: parse + authenticate + delegate. `PrepublishView` (the PostPublishDataset callback, IP-034 Q4) hands the
dataset to the generic sync (`apps.harvest.sync.apply_record`) of the enabled `dataverse` harvest source, or to the
legacy `apply_inbound` when there is none; `ValidateView` (PrePublishDataset `http/sr`, Q9) answers `OK` or
`FAILED: <reason>` by the connector's rules. The "Open in Elvira" link is the generic `/resolve/dataverse?id=`.
"""

import hmac
import json
import logging
from http import HTTPStatus

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps import openapi
from apps.core.errors import ProblemDetailException
from apps.dataverse.exceptions import (
    DataverseAuthError,
    DataverseConfigError,
    DataverseError,
)
from apps.dataverse.services.router import CatalogRouter
from apps.dataverse.services.sync import DataverseSyncService, PrepublishPayload, apply_inbound
from apps.dataverse.services.validate import validate_dataset
from apps.dataverse.services.workflow import schedule_workflow_resume

logger = logging.getLogger(__name__)

_TEXT = "text/plain; charset=utf-8"


def _workflow_payload(request, what: str) -> PrepublishPayload:
    """The JSON a Dataverse `http/sr` step sends, authenticated by the shared workflow secret."""
    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        payload_dict = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=exc)

    # Authn: shared secret (per current contract — HMAC migration is a future hardening item).
    expected_secret = settings.EVILFLOWERS_DATAVERSE_WORKFLOW_SECRET
    provided_secret = payload_dict.get("secret", "") if isinstance(payload_dict, dict) else ""
    if not isinstance(provided_secret, str):
        provided_secret = ""
    if not expected_secret or not hmac.compare_digest(provided_secret, expected_secret):
        logger.warning("Dataverse %s secret validation failed", what)
        raise ProblemDetailException(_("Forbidden"), status=HTTPStatus.FORBIDDEN)

    try:
        return PrepublishPayload.from_dict(payload_dict)
    except DataverseError as exc:
        raise ProblemDetailException(str(exc), status=HTTPStatus.BAD_REQUEST, previous=exc)


def sync_source(payload: PrepublishPayload):
    """The enabled harvest source of the `dataverse` connector this dataset belongs to: the only one, or among
    several the one whose target catalog the `CatalogRouter` picks (else the oldest). None: no source."""
    from apps.harvest.models import HarvestSource

    sources = list(
        HarvestSource.objects.filter(adapter="dataverse", enabled=True)
        .select_related("target_catalog")
        .order_by("created_at")
    )
    if len(sources) <= 1:
        return sources[0] if sources else None
    catalog = CatalogRouter().resolve(dataset_id=payload.dataset_id, global_id=payload.global_id).catalog
    return next((source for source in sources if source.target_catalog_id == catalog.pk), sources[0])


@method_decorator(csrf_exempt, name="dispatch")
class PrepublishView(View):
    """`POST /api/v1/dataverse-prepublish` — workflow callback (PostPublishDataset since IP-034)."""

    @openapi.metadata(
        description=(
            "Dataverse workflow callback (a PostPublishDataset `http/sr` step): reads the dataset's latest "
            "published version and applies it to Elvira. Body: `secret`, `dataset_id`, `global_id`, optional "
            "`invocation_id` and `title`."
        ),
        tags=["Dataverse"],
    )
    def post(self, request, invocation_id=None):
        payload = _workflow_payload(request, "prepublish")

        # Bind URL-kwarg fallback (legacy callers passed invocation_id on the path).
        if not payload.invocation_id and invocation_id:
            payload = PrepublishPayload(
                dataset_id=payload.dataset_id,
                global_id=payload.global_id,
                title=payload.title,
                invocation_id=invocation_id,
            )

        logger.info(
            "Dataverse prepublish dataset_id=%s global_id=%s title=%s invocation_id=%s",
            payload.dataset_id,
            payload.global_id,
            payload.title,
            payload.invocation_id,
        )

        try:
            client = DataverseSyncService._default_client()
            source = sync_source(payload)
            with transaction.atomic():
                if source is not None:
                    # IP-034: the generic sync (fetch :latest-published, match, merge, conflicts, withdraw).
                    from apps.harvest.sync import apply_record

                    outcome = apply_record(source, payload.global_id)
                    logger.info(
                        "Dataverse prepublish source=%s pid=%s status=%s",
                        source.slug,
                        payload.global_id,
                        getattr(outcome, "status", None),
                    )
                else:
                    # No enabled `dataverse` harvest source: the pre-IP-034 import keeps working.
                    apply_inbound(payload.dataset_id, payload.global_id, title=payload.title, client=client)
                # IP-008 Phase 4 D2: enqueue resume on commit only — so a
                # rolled-back sync never triggers the upstream workflow.
                schedule_workflow_resume(
                    dataverse_base_url=client.base_url,
                    dataverse_token=client.token,
                    invocation_id=payload.invocation_id,
                )
        except DataverseConfigError as exc:
            logger.error("Dataverse prepublish misconfigured: %s", exc)
            raise ProblemDetailException(
                _("Dataverse integration misconfigured"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail=str(exc),
                previous=exc,
            )
        except DataverseAuthError as exc:
            raise ProblemDetailException(
                _("Dataverse authentication failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(exc),
                previous=exc,
            )
        except DataverseError as exc:
            raise ProblemDetailException(
                _("Dataverse integration failed"),
                status=HTTPStatus.BAD_GATEWAY,
                detail=str(exc),
                previous=exc,
            )

        return HttpResponse("OK", status=HTTPStatus.OK, content_type=_TEXT)


@method_decorator(csrf_exempt, name="dispatch")
class ValidateView(View):
    """`POST /api/v1/dataverse-validate` — IP-034 Q9, a PrePublishDataset `http/sr` step."""

    @openapi.metadata(
        description=(
            "Pre-publish check of `book` datasets, for a PrePublishDataset `http/sr` step with `expectedResponse` "
            "`OK.*`. Body as for the prepublish callback (`secret`, `dataset_id`, `global_id`, `invocation_id`). "
            "Answers 200 `text/plain`: `OK`, or `FAILED: <reason>` (an invalid ISBN, no UDC code, or a dataset Elvira "
            "could not read), which fails the step and stops the publish. Datasets of other types answer `OK`."
        ),
        tags=["Dataverse"],
    )
    def post(self, request):
        payload = _workflow_payload(request, "validate")
        try:
            client = DataverseSyncService._default_client()
        except DataverseConfigError as exc:
            logger.error("Dataverse validate misconfigured: %s", exc)
            return HttpResponse("FAILED: Elvira cannot read Dataverse (no API token)", content_type=_TEXT)
        verdict = validate_dataset(client, payload.dataset_id, source=sync_source(payload))
        if not verdict.ok:
            logger.warning(
                "dataverse.validate refused dataset_id=%s global_id=%s reason=%s",
                payload.dataset_id,
                payload.global_id,
                verdict.reason,
            )
        return HttpResponse(verdict.text, status=HTTPStatus.OK, content_type=_TEXT)


__all__ = ["PrepublishView", "ValidateView", "sync_source"]

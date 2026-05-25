"""
Dataverse pre-publish webhook view.

Thin: parses + authenticates + delegates to `DataverseSyncService`.
The legacy 500-line god method now lives in `services/sync.py` and
its helpers.
"""

import hmac
import json
import logging
import os
from http import HTTPStatus

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
from apps.dataverse.services.sync import DataverseSyncService, PrepublishPayload
from apps.dataverse.services.workflow import schedule_workflow_resume

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class PrepublishView(View):
    """`POST /api/v1/dataverse-prepublish` — workflow callback."""

    @openapi.metadata(description="Dataverse workflow pre-publish ingest", tags=["Dataverse"])
    def post(self, request, invocation_id=None):
        try:
            raw = request.body.decode("utf-8") if request.body else "{}"
            payload_dict = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProblemDetailException(_("Invalid JSON payload"), status=HTTPStatus.BAD_REQUEST, previous=exc)

        # Authn: shared secret (per current contract — HMAC migration
        # is a future hardening item; out of scope for IP-008).
        expected_secret = os.getenv("DATAVERSE_WORKFLOW_SECRET", "")
        provided_secret = payload_dict.get("secret", "") if isinstance(payload_dict, dict) else ""
        if not expected_secret or not hmac.compare_digest(provided_secret, expected_secret):
            logger.warning("Dataverse prepublish secret validation failed")
            raise ProblemDetailException(_("Forbidden"), status=HTTPStatus.FORBIDDEN)

        try:
            payload = PrepublishPayload.from_dict(payload_dict)
        except DataverseError as exc:
            raise ProblemDetailException(str(exc), status=HTTPStatus.BAD_REQUEST, previous=exc)

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
            service = DataverseSyncService()
            with transaction.atomic():
                service.sync(payload)
                # IP-008 Phase 4 D2: enqueue resume on commit only — so a
                # rolled-back sync never triggers the upstream workflow.
                schedule_workflow_resume(
                    dataverse_base_url=service.client.base_url,
                    dataverse_token=service.client.token,
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

        return HttpResponse("OK", status=HTTPStatus.OK, content_type="text/plain; charset=utf-8")


__all__ = ["PrepublishView"]

"""
Encryption Management Views

Encryption status and manual triggering for Readium LCP, per entry. Every trigger goes through the
job service (IP-031, kind `readium.encrypt`), so it is recorded in the job ledger like any other job.
"""

import logging
from http import HTTPStatus
from uuid import UUID

from django.utils.translation import gettext as _

from apps import openapi
from apps.api.response import SingleResponse
from apps.core.errors import ProblemDetailException, ValidationException, DetailType
from apps.core.models import Entry, Acquisition
from apps.core.views import SecuredView
from apps.readium.forms import EncryptionTriggerForm
from apps.readium.models import EncryptedContent
from apps.readium.services import ContentEncryptionService
from apps.readium.serializers import EncryptionJobSerializer
from apps.tasks.application.facade import facade
from apps.tasks.contract import DispatchError
from apps.tasks.models import Job

logger = logging.getLogger(__name__)

ENCRYPTABLE = (Acquisition.AcquisitionMIME.EPUB, Acquisition.AcquisitionMIME.PDF)


def _entry(entry_id: UUID) -> Entry:
    try:
        return Entry.objects.get(pk=entry_id)
    except Entry.DoesNotExist:
        raise ProblemDetailException(
            _("Entry not found"),
            status=HTTPStatus.NOT_FOUND,
            detail_type=DetailType.NOT_FOUND,
        )


def _refused(error: DispatchError) -> ProblemDetailException:
    # A helper, not inline: `apps.openapi` reads `status=HTTPStatus.<NAME>` literally from view code.
    return ProblemDetailException(error.message, status=error.status, previous=error)


def _encryption(acquisition: Acquisition, job_ids):
    try:
        encrypted_content = acquisition.encrypted_content
    except EncryptedContent.DoesNotExist:
        return None
    return {
        **EncryptionJobSerializer.Job.view(encrypted_content, job_ids.get(acquisition.pk)),
        # Kept from the original response shape.
        "lcp_content_id": encrypted_content.lcp_content_id,
        "ready_for_licensing": ContentEncryptionService.is_ready_for_licensing(acquisition),
    }


class EntryEncryptionView(SecuredView):
    """Manage encryption for entry content."""

    @openapi.metadata(
        description=(
            "**Deprecated** (IP-031 Q11): `GET /api/v1/jobs?kind=readium.encrypt&entry_id=…` lists the same jobs. "
            "Encryption status of the entry's PDF/EPUB files. `acquisitions` lists every file with its encryption "
            "job (task id, trace id, attempts, report, stale, stuck, job_id of /api/v1/jobs); `encryption` is the "
            "first file's, as before. Existing jobs are listed even when LCP is switched off for the entry."
        ),
        tags=["Readium", "Encryption"],
        summary="Get encryption status",
    )
    def get(self, request, entry_id: UUID):
        entry = _entry(entry_id)
        readium_enabled = bool(entry.read_config("readium_enabled"))

        acquisitions = list(
            entry.acquisitions.filter(mime__in=ENCRYPTABLE).select_related("encrypted_content").order_by("created_at")
        )
        # The `readium.encrypt` job of each file in /api/v1/jobs.
        job_ids = dict(
            Job.objects.filter(kind="readium.encrypt", subject_id__in=[a.pk for a in acquisitions]).values_list(
                "subject_id", "pk"
            )
        )
        files = [
            {
                "acquisition_id": str(acquisition.pk),
                "mime": acquisition.mime,
                "encryption": _encryption(acquisition, job_ids),
            }
            for acquisition in acquisitions
        ]
        data = {
            "readium_enabled": readium_enabled,
            "has_acquisition": bool(files),
            "encryption": files[0]["encryption"] if files else None,
            "acquisitions": files,
        }
        if not readium_enabled:
            data["message"] = "Readium is not enabled for this entry"
        elif not files:
            data["message"] = "No suitable acquisition found"
        elif files[0]["encryption"] is None:
            data["message"] = "Encryption not started"
        return SingleResponse(request, data=EncryptionJobSerializer.EntryStatus.model_validate(data))

    @openapi.metadata(
        description="""
        Trigger encryption of the entry's PDF/EPUB files (`acquisition_id`: one of them).

        **Deprecated** (IP-031 Q11): declare `readium.encrypt` jobs with `POST /api/v1/jobs` instead.

        - Not encrypted yet, `pending` (stale) or `failed`: (re)sent under the same content id; a job still on
          its way is left alone.
        - `completed` / `registered`: 409, unless `force`, which re-encrypts in place (same content id). With
          active loans that answers 409 with their count. To re-encrypt anyway, apply
          `{kind: readium.encrypt, subject_id, spec: {reencrypt: true, force: true}}` to `/api/v1/jobs`.

        Requires admin permissions.
        """,
        tags=["Readium", "Encryption"],
        summary="Trigger encryption",
    )
    def post(self, request, entry_id: UUID):
        # Require admin permission for manual encryption trigger
        if not request.user.has_perm("core.change_entry"):
            raise ProblemDetailException(
                _("Insufficient permissions"),
                status=HTTPStatus.FORBIDDEN,
            )

        entry = _entry(entry_id)

        if not entry.read_config("readium_enabled"):
            raise ProblemDetailException(
                _("Readium is not enabled for this entry"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        form = EncryptionTriggerForm.create_from_request(request)
        if not form.is_valid():
            raise ValidationException(form)
        force = form.cleaned_data.get("force", False)
        acquisition_id = form.cleaned_data.get("acquisition_id")

        acquisitions = entry.acquisitions.filter(mime__in=ENCRYPTABLE).order_by("created_at")
        if acquisition_id:
            acquisitions = acquisitions.filter(pk=acquisition_id)
        acquisitions = list(acquisitions)
        if not acquisitions:
            raise ProblemDetailException(
                _("No suitable acquisition found for encryption"),
                status=HTTPStatus.BAD_REQUEST,
                detail_type=DetailType.VALIDATION_ERROR,
            )

        # Check every file before declaring any, so a refusal never leaves half the entry re-queued.
        plans = []
        for acquisition in acquisitions:
            encrypted_content = getattr(acquisition, "encrypted_content", None)
            done = encrypted_content is not None and encrypted_content.status in (
                EncryptedContent.EncryptionStatus.COMPLETED,
                EncryptedContent.EncryptionStatus.REGISTERED,
            )
            if done and not force:
                raise ProblemDetailException(
                    _("Encryption already in progress or completed. Use force=true to re-trigger."),
                    status=HTTPStatus.CONFLICT,
                    detail_type=DetailType.CONFLICT,
                )
            if done:
                loans = ContentEncryptionService.active_licenses(encrypted_content).count()
                if loans:
                    # #148: never delete rows licenses reference; re-encrypting would break these loans.
                    raise ProblemDetailException(
                        _("%(count)d active loan(s) would break; re-encrypting is refused.") % {"count": loans},
                        status=HTTPStatus.CONFLICT,
                        detail_type=DetailType.CONFLICT,
                        additional_data={"active_loans": loans, "acquisition_id": str(acquisition.pk)},
                    )
            plans.append((acquisition, {"reencrypt": True} if done else {}))

        # Declared, not sent: an unchanged spec on a job still on its way changes nothing (no second
        # lcpencrypt run on one package); a stale or failed one gets a new run.
        jobs = []
        for acquisition, spec in plans:
            try:
                job = facade.apply("readium.encrypt", acquisition.pk, spec=spec, user=request.user)
            except DispatchError as error:
                raise _refused(error)
            acquisition.refresh_from_db()
            encrypted_content = acquisition.encrypted_content
            jobs.append(
                {
                    "acquisition_id": str(acquisition.pk),
                    "job_id": str(job.pk),
                    "state": job.state,
                    "lcp_content_id": encrypted_content.lcp_content_id,
                    "status": encrypted_content.status,
                }
            )

        triggered = {
            "message": "Encryption triggered successfully",
            # Kept from the original response shape: the first file.
            "lcp_content_id": jobs[0]["lcp_content_id"],
            "status": jobs[0]["status"],
            "jobs": jobs,
        }
        return SingleResponse(
            request,
            data=EncryptionJobSerializer.EntryTriggered.model_validate(triggered),
            status=HTTPStatus.ACCEPTED,
        )

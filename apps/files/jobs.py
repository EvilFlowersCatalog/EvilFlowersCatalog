"""OCR job kind (#146, IP-031): make a scanned PDF searchable.

The worker (`evilflowers-ocr-service`) reads the acquisition's file from shared storage, runs
`ocrmypdf(skip_text=True)` into `<entry dir>/ocr/<run id>.pdf` and reports to the `report_url` the task
carries (`POST /api/v1/jobs/runs/<run id>/report`). It never writes the original. The report decides (Q3):

- `evilflowers_ocr_rewrite` on: the acquisition's file becomes the OCR output, its checksum is cleared,
  and LCP re-encryption and extraction are declared as follow-up jobs. The original stays where it was
  (named in the report) until the next successful OCR of the same file.
- off: the output becomes a new, derived acquisition; the original is untouched.

A `skipped` report (the PDF already had text) changes nothing.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.tasks.contract import (
    Blocked,
    Dispatch,
    DispatchError,
    JobDefinition,
    Ready,
    Refused,
    Report,
    Spec,
    declare,
    entry_scope,
    registry,
    uuid_arg,
)

logger = logging.getLogger(__name__)

REPORT_STATUSES = ("done", "skipped", "failed")


class OcrSpec(Spec):
    #: Redo OCR on pages that already have text (ocrmypdf `redo_ocr`).
    redo: bool = False


def _destination(acquisition, run_id) -> str:
    return f"{acquisition.upload_base_path()}/ocr/{run_id}.pdf"


@registry.register
class OcrDefinition(JobDefinition[Acquisition, OcrSpec]):
    kind = "files.ocr"
    title = "OCR"
    description = (
        "Add a text layer to a scanned PDF with ocrmypdf (pages that already have text are skipped). The entry's "
        "`evilflowers_ocr_rewrite` decides whether the result replaces the file or is added as a new acquisition."
    )
    task = "evilflowers_ocr_worker.ocr"
    subject_model = Acquisition
    spec_model = OcrSpec
    reports = True

    @property
    def queue(self):
        return settings.EVILFLOWERS_OCR_QUEUE

    @property
    def time_limit(self):
        return timedelta(seconds=settings.EVILFLOWERS_OCR_TIME_LIMIT)

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg([], kwargs, "acquisition_id")

    def check(self, acquisition, spec):
        if acquisition.mime != Acquisition.AcquisitionMIME.PDF:
            return Refused(_("Only PDF files are OCR'd."))
        if acquisition.storage_backend != Acquisition.StorageBackend.LOCAL or not acquisition.content:
            return Refused(_("The acquisition has no stored file."))
        if not settings.EVILFLOWERS_OCR_ENABLED:
            return Blocked(_("OCR is switched off: no OCR worker is deployed (EVILFLOWERS_OCR_ENABLED)."))
        language = acquisition.entry.language
        if language is None or not getattr(language, "alpha3", None):
            return Blocked(_("Set the entry's language first; OCR needs it."))
        return Ready()

    def kwargs(self, dispatch: Dispatch[Acquisition, OcrSpec]) -> dict:
        acquisition = dispatch.subject
        base = settings.EVILFLOWERS_JOBS_REPORT_BASE_URL.rstrip("/")
        return {
            "job_id": dispatch.job_id,
            "run_id": dispatch.run_id,
            "acquisition_id": str(acquisition.pk),
            "source": acquisition.content.name,
            "destination": _destination(acquisition, dispatch.run_id),
            "language": acquisition.entry.language.alpha3,
            "redo": dispatch.spec.redo,
            "report_url": f"{base}/api/v1/jobs/runs/{dispatch.run_id}/report",
        }

    def on_report(self, report: Report[Acquisition]):
        from apps.files.storage import get_storage

        payload, acquisition = report.payload, report.subject
        status = payload.get("status")
        if status not in REPORT_STATUSES:
            raise DispatchError(
                _("`status` must be one of: %(statuses)s.") % {"statuses": ", ".join(REPORT_STATUSES)}, status=400
            )
        stored = {
            key: payload[key]
            for key in ("status", "pages", "pages_ocred", "skipped_reason", "error", "warnings")
            if payload.get(key) is not None
        }
        if status != "done":
            return stored

        destination = report.run_kwargs.get("destination") or _destination(acquisition, report.run_id)
        if payload.get("output") and payload["output"] != destination:
            raise DispatchError(_("The report names an output other than the run's destination."), status=400)
        if not get_storage().exists(destination):
            raise DispatchError(_("The OCR output %(path)s does not exist.") % {"path": destination}, status=400)

        stored["output"] = destination
        if acquisition.entry.read_config("evilflowers_ocr_rewrite"):
            stored.update(self._replace(report, acquisition, destination))
        else:
            stored.update(self._derive(acquisition, destination))
        return stored

    def _replace(self, report: Report, acquisition, destination) -> dict:
        from apps.files.storage import get_storage
        from apps.tasks.models import JobRun

        original = acquisition.content.name
        # The original kept by the previous successful OCR of this file goes now (Q3).
        previous = (
            JobRun.objects.filter(kind=self.kind, subject_id=acquisition.pk, reported_at__isnull=False)
            .exclude(pk=report.run_id)
            .order_by("-reported_at")
            .values_list("report", flat=True)
            .first()
        )
        stale_original = (previous or {}).get("original")
        if stale_original and stale_original not in (original, destination):
            try:
                storage = get_storage()
                if storage.exists(stale_original):
                    storage.delete(stale_original)
            except Exception:
                logger.exception("ocr.original_not_deleted path=%s", stale_original)

        acquisition.content.name = destination
        acquisition.checksum_cached = None
        acquisition.save(update_fields=["content", "checksum_cached", "updated_at"])
        # The new file is declared, not sent: LCP re-encryption and extraction become jobs of their own.
        transaction.on_commit(lambda: _follow_up(acquisition.pk, report.job_id))
        return {"applied": "replaced", "original": original}

    @staticmethod
    def _derive(acquisition, destination) -> dict:
        derived = Acquisition(
            entry=acquisition.entry, relation=acquisition.relation, mime=Acquisition.AcquisitionMIME.PDF
        )
        derived.content.name = destination
        derived.save()
        return {"applied": "derived", "derived_acquisition_id": str(derived.pk)}


def _follow_up(acquisition_id, cause_id) -> None:
    acquisition = Acquisition.objects.filter(pk=acquisition_id).select_related("entry").first()
    if acquisition is None:
        return
    declarations = []
    if acquisition.entry.read_config("readium_enabled"):
        has_package = Acquisition.objects.filter(pk=acquisition_id, encrypted_content__isnull=False).exists()
        declarations.append(("readium.encrypt", {"reencrypt": True} if has_package else {}))
    if settings.EVILFLOWERS_EXTRACTION_ENABLED:
        declarations.append(("extraction.run", {}))
    for kind, spec in declarations:
        try:
            declare(kind, acquisition_id, spec=spec, cause_id=cause_id)
        except DispatchError as error:
            logger.warning(
                "ocr.follow_up_refused kind=%s acquisition=%s reason=%s", kind, acquisition_id, error.message
            )


def queue_ocr(acquisition: Acquisition) -> None:
    """Declare OCR for a new upload when the entry asks for it (`evilflowers_ocr_enabled`), after commit.

    Never raises: OCR is best-effort and must not fail an upload.
    """
    if not settings.EVILFLOWERS_OCR_ENABLED or not acquisition.entry.read_config("evilflowers_ocr_enabled"):
        return

    def _declare(acquisition_id=acquisition.pk):
        try:
            declare(OcrDefinition.kind, acquisition_id)
        except DispatchError as error:
            logger.info("ocr.not_declared acquisition=%s reason=%s", acquisition_id, error.message)
        except Exception:
            logger.exception("ocr.not_declared acquisition=%s", acquisition_id)

    transaction.on_commit(_declare)


__all__ = ["OcrDefinition", "queue_ocr"]

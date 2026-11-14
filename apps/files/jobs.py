"""OCR job kind (#146, IP-031): make a scanned PDF searchable.

The worker (`evilflowers-ocr-worker`) reads the acquisition's file from shared storage, runs
`ocrmypdf(skip_text=True)` into `<entry dir>/ocr/<job id>.pdf` and reports to
`POST /api/v1/jobs/<job id>/report`. It never writes the original. The report decides (Q3):

- `evilflowers_ocr_rewrite` on: the acquisition's file becomes the OCR output, its checksum is cleared,
  and LCP encryption (re-encrypt, same content id) and extraction run again as child jobs. The
  original stays where it was (named in the report) until the next successful OCR of the same file.
- off: the output becomes a new, derived acquisition of the entry; the original is untouched.

A `skipped` report (the PDF already had text) changes nothing.
"""

import logging
import os
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.tasks.kinds import JobKind, JobRefused, entry_scope, first_arg, registry

logger = logging.getLogger(__name__)

REPORT_STATUSES = ("done", "skipped", "failed")


def _destination(acquisition, job) -> str:
    return f"{acquisition.upload_base_path()}/ocr/{job.pk}.pdf"


@registry.register
class OcrKind(JobKind):
    name = "files.ocr"
    title = "OCR"
    description = (
        "Add a text layer to a scanned PDF with ocrmypdf (pages that already have text are skipped). "
        "The entry's `evilflowers_ocr_rewrite` decides whether the result replaces the file or is added "
        "as a new acquisition."
    )
    task = "evilflowers_ocr_worker.ocr"
    subject_model = Acquisition
    reports = True
    options_schema = {
        "type": "object",
        "properties": {
            "redo": {"type": "boolean", "description": "Redo OCR on pages that already have text (redo_ocr)."}
        },
        "additionalProperties": False,
    }

    @property
    def queue(self):
        return settings.EVILFLOWERS_OCR_QUEUE

    @property
    def time_limit(self):
        return timedelta(seconds=settings.EVILFLOWERS_OCR_TIME_LIMIT)

    def subject_from_call(self, args, kwargs):
        return first_arg([], kwargs, "acquisition_id")

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def eligible(self, acquisition, options):
        if not settings.EVILFLOWERS_OCR_ENABLED:
            return _("OCR is switched off: no OCR worker is deployed (EVILFLOWERS_OCR_ENABLED).")
        if acquisition.mime != Acquisition.AcquisitionMIME.PDF:
            return _("Only PDF files are OCR'd.")
        if acquisition.storage_backend != Acquisition.StorageBackend.LOCAL or not acquisition.content:
            return _("The acquisition has no stored file.")
        language = acquisition.entry.language
        if language is None or not getattr(language, "alpha3", None):
            return _("Set the entry's language first; OCR needs it.")
        return None

    def kwargs(self, job, acquisition, options):
        base = settings.EVILFLOWERS_JOBS_REPORT_BASE_URL.rstrip("/")
        return {
            "job_id": str(job.pk),
            "acquisition_id": str(acquisition.pk),
            "source": acquisition.content.name,
            "destination": _destination(acquisition, job),
            "language": acquisition.entry.language.alpha3,
            "redo": bool(options.get("redo")),
            "report_url": f"{base}/api/v1/jobs/{job.pk}/report",
        }

    def on_report(self, job, acquisition, payload):
        from apps.files.storage import get_storage

        status = (payload or {}).get("status")
        if status not in REPORT_STATUSES:
            raise JobRefused(
                _("`status` must be one of: %(statuses)s.") % {"statuses": ", ".join(REPORT_STATUSES)}, status=400
            )
        report = {
            key: payload.get(key)
            for key in ("status", "pages", "pages_ocred", "skipped_reason", "error", "warnings")
            if payload.get(key) is not None
        }
        if status != "done":
            return report

        destination = (job.job_kwargs or {}).get("destination") or _destination(acquisition, job)
        if payload.get("output") and payload["output"] != destination:
            raise JobRefused(_("The report names an output other than the job's destination."), status=400)
        if not get_storage().exists(destination):
            raise JobRefused(_("The OCR output %(path)s does not exist.") % {"path": destination}, status=400)

        report["output"] = destination
        if acquisition.entry.read_config("evilflowers_ocr_rewrite"):
            report.update(self._replace(job, acquisition, destination))
        else:
            report.update(self._derive(acquisition, destination))
        return report

    def _replace(self, job, acquisition, destination) -> dict:
        from apps.files.storage import get_storage
        from apps.tasks.models import JobProtocol

        original = acquisition.content.name
        # The original kept by the previous successful OCR of this file goes now (Q3).
        previous = (
            JobProtocol.objects.filter(kind=self.name, subject_id=acquisition.pk, reported_at__isnull=False)
            .exclude(pk=job.pk)
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
        transaction.on_commit(lambda: _follow_up(job.pk, acquisition.pk))
        return {"applied": "replaced", "original": original}

    def _derive(self, acquisition, destination) -> dict:
        derived = Acquisition(
            entry=acquisition.entry,
            relation=acquisition.relation,
            mime=Acquisition.AcquisitionMIME.PDF,
        )
        derived.content.name = destination
        derived.save()
        return {"applied": "derived", "derived_acquisition_id": str(derived.pk)}

    def detail(self, job, acquisition):
        return {"output": (job.job_kwargs or {}).get("destination")}


def _follow_up(job_id, acquisition_id) -> None:
    """After a replaced file: LCP re-encryption and extraction of the new file, as child jobs."""
    from apps.tasks.models import JobProtocol
    from apps.tasks.services import JobService

    parent = JobProtocol.objects.filter(pk=job_id).first()
    acquisition = Acquisition.objects.filter(pk=acquisition_id).select_related("entry").first()
    if parent is None or acquisition is None:
        return

    started = {}
    if acquisition.entry.read_config("readium_enabled"):
        has_package = Acquisition.objects.filter(pk=acquisition.pk, encrypted_content__isnull=False).exists()
        started["readium.encrypt"] = {"reencrypt": True, "force": False} if has_package else {}
    if settings.EVILFLOWERS_EXTRACTION_ENABLED:
        started["extraction.run"] = {}

    results = {}
    for kind, options in started.items():
        try:
            child = JobService.start(
                kind, acquisition.pk, options=options, force=True, parent=parent, check_permission=False
            )
            results[kind] = str(child.pk)
        except JobRefused as error:
            # E.g. active loans block the re-encryption: the operator decides (reencrypt action, force).
            results[kind] = f"refused: {error.message}"
            logger.warning("ocr.follow_up_refused job=%s kind=%s reason=%s", job_id, kind, error.message)
    if results:
        report = dict(parent.report or {})
        report["follow_ups"] = results
        JobProtocol.objects.filter(pk=job_id).update(report=report)


def queue_ocr(acquisition: Acquisition) -> None:
    """Start OCR for a new upload when the entry asks for it (`evilflowers_ocr_enabled`), after commit.

    Never raises: OCR is best-effort and must not fail an upload.
    """
    if not settings.EVILFLOWERS_OCR_ENABLED or not acquisition.entry.read_config("evilflowers_ocr_enabled"):
        return
    if OcrKind().eligible(acquisition, {}):
        return

    def _start(acquisition_id=acquisition.pk):
        from apps.tasks.services import JobService

        try:
            JobService.start(OcrKind.name, acquisition_id, check_permission=False)
        except JobRefused as error:
            logger.info("ocr.not_started acquisition=%s reason=%s", acquisition_id, error.message)
        except Exception:
            logger.exception("ocr.not_started acquisition=%s", acquisition_id)

    transaction.on_commit(_start)


__all__ = ["OcrKind", "queue_ocr"]

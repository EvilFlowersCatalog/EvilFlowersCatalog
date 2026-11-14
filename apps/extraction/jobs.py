"""Extraction job kinds (IP-031): the analyzer run (IP-018) and its post-processing (IP-030)."""

from django.conf import settings
from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.tasks.kinds import JobKind, JobRefused, entry_scope, first_arg, registry


@registry.register
class ExtractionRunKind(JobKind):
    name = "extraction.run"
    title = "AI processing (extraction)"
    description = (
        "Extract figures, tables, equations and the paragraph index of a PDF (IP-018). The analyzer fans "
        "out to the image, equation and text workers; their tasks appear as children of this job."
    )
    task = "evilflowers_analyzer.extract"
    subject_model = Acquisition
    # The task id is a function of acquisition and pipeline version (`services.task_id`): workers'
    # late reports are told apart by `attempt`, not by task id (Q9).
    stable_task_id = True
    options_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def queue(self):
        return settings.EVILFLOWERS_EXTRACTION_QUEUE

    def subject_from_call(self, args, kwargs):
        return first_arg([], kwargs, "acquisition_id")

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def eligible(self, acquisition, options):
        from apps.extraction.services import is_extractable

        if not settings.EVILFLOWERS_EXTRACTION_ENABLED:
            return _("Extraction is switched off (EVILFLOWERS_EXTRACTION_ENABLED).")
        if not is_extractable(acquisition):
            return _("Only stored PDF files are extracted.")
        return None

    def task_id_for(self, acquisition, options):
        from apps.extraction.services import task_id

        return task_id(acquisition.pk, settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION)

    def dispatch(self, job, acquisition, options):
        from apps.extraction import services

        # JobService refused a second active job already; `force` re-runs a done or stuck run.
        if services.queue(acquisition, force=True) is None:
            raise JobRefused(_("The extraction run could not be queued; see the catalog log."), status=502)

    def detail(self, job, acquisition):
        from apps.extraction.models import ExtractionRun

        if acquisition is None:
            return {}
        run = (
            ExtractionRun.objects.filter(
                acquisition=acquisition, pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
            )
            .only("id", "status", "attempt", "jobs", "error", "is_active", "started_at", "finished_at")
            .first()
        )
        if run is None:
            return {}
        return {
            "extraction_run": {
                "id": str(run.pk),
                "status": run.status,
                "attempt": run.attempt,
                "jobs": run.jobs,
                "error": run.error,
                "is_active": run.is_active,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        }


@registry.register
class PostProcessKind(JobKind):
    name = "extraction.post_process"
    title = "Extraction post-processing"
    description = "Cross-references, figure documents and vectors, and the graph projection of an active run."
    task = "apps.extraction.tasks.post_process_run"

    @property
    def subject_model(self):
        from apps.extraction.models import ExtractionRun

        return ExtractionRun

    def subject_from_call(self, args, kwargs):
        return first_arg(args, kwargs, "run_id")

    def scope(self, run):
        return entry_scope(run.acquisition.entry)

    def eligible(self, run, options):
        if not run.is_active:
            return _("Only the active run of an acquisition is post-processed.")
        return None

    def args(self, job, run, options):
        return [str(run.pk)]

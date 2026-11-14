"""Extraction job kinds (IP-031): the analyzer run (IP-018) and its post-processing (IP-030).

The run's truth is `ExtractionRun`: the definition observes it, and summarises its jobs per kind
(`figures:0001-0008` → `figures`), because with per-batch dispatch a book has hundreds of them (#150).
"""

from django.conf import settings
from django.utils.translation import gettext as _

from apps.core.models import Acquisition
from apps.extraction.batches import summarise_jobs
from apps.tasks.contract import (
    Blocked,
    DispatchError,
    JobDefinition,
    JobState,
    Observation,
    Ready,
    Refused,
    Spec,
    entry_scope,
    registry,
    uuid_arg,
)


def _current_run(acquisition):
    from apps.extraction.models import ExtractionRun

    return ExtractionRun.objects.filter(
        acquisition=acquisition, pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
    ).first()


@registry.register
class ExtractionRunDefinition(JobDefinition[Acquisition, Spec]):
    kind = "extraction.run"
    title = "AI processing (extraction)"
    description = (
        "Extract figures, tables, equations and the paragraph index of a PDF (IP-018). The analyzer fans out to "
        "the image, equation and text workers; their tasks are runs of this job."
    )
    task = "evilflowers_analyzer.extract"
    subject_model = Acquisition
    # The task id is a function of acquisition and pipeline version: workers' late reports are told apart
    # by `attempt`, so every run of the job reuses one run row (Q9).
    stable_task_id = True

    @property
    def queue(self):
        return settings.EVILFLOWERS_EXTRACTION_QUEUE

    @property
    def observes(self):
        from apps.extraction.models import ExtractionRun

        return (ExtractionRun,)

    def scope(self, acquisition):
        return entry_scope(acquisition.entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg([], kwargs, "acquisition_id")

    def subject_of(self, run):
        if run.pipeline_version != settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION:
            return None
        return run.acquisition

    def check(self, acquisition, spec):
        from apps.extraction.services import is_extractable

        if not is_extractable(acquisition):
            return Refused(_("Only stored PDF files are extracted."))
        if not settings.EVILFLOWERS_EXTRACTION_ENABLED:
            return Blocked(_("Extraction is switched off (EVILFLOWERS_EXTRACTION_ENABLED)."))
        return Ready()

    def task_id_for(self, acquisition, spec):
        from apps.extraction.services import task_id

        return task_id(acquisition.pk, settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION)

    def dispatch(self, dispatch):
        from apps.extraction import services

        # `force`: the facade decided this generation needs a run; a done or stuck run starts over.
        if services.queue(dispatch.subject, force=True) is None:
            raise DispatchError(_("The extraction run could not be queued; see the catalog log."), status=502)

    def observe(self, acquisition):
        from apps.extraction.models import ExtractionRun

        run = _current_run(acquisition)
        if run is None:
            return None
        state = {
            ExtractionRun.Status.QUEUED: JobState.QUEUED,
            ExtractionRun.Status.RUNNING: JobState.RUNNING,
            ExtractionRun.Status.DONE: JobState.SUCCEEDED,
            ExtractionRun.Status.FAILED: JobState.FAILED,
        }[run.status]
        detail = {
            "extraction_run": {
                "id": str(run.pk),
                "status": run.status,
                "attempt": run.attempt,
                "is_active": run.is_active,
                "jobs": summarise_jobs(run.jobs),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        }
        return Observation(
            state=state, reason=run.error if state is JobState.FAILED else None, detail=detail, since=run.updated_at
        )

    def discover(self):
        return Acquisition.objects.filter(
            extraction_runs__pipeline_version=settings.EVILFLOWERS_EXTRACTION_PIPELINE_VERSION
        ).select_related("entry")


@registry.register
class PostProcessDefinition(JobDefinition):
    kind = "extraction.post_process"
    title = "Extraction post-processing"
    description = "Cross-references, figure documents and vectors, and the graph projection of an active run."
    task = "apps.extraction.tasks.post_process_run"

    @property
    def subject_model(self):
        from apps.extraction.models import ExtractionRun

        return ExtractionRun

    def scope(self, run):
        return entry_scope(run.acquisition.entry)

    def subject_from_call(self, args, kwargs):
        return uuid_arg(args, kwargs, "run_id")

    def check(self, run, spec):
        return Ready() if run.is_active else Refused(_("Only the active run of an acquisition is post-processed."))

    def args(self, dispatch):
        return [str(dispatch.subject.pk)]
